import argparse
import datetime
import os

from kg_grafana import GrafanaDashboardSource_GNet, GrafanaDashboardSource_Url
from kg_prometheus import PrometheusConfigFile, PrometheusConfigFileOptions, PrometheusConfigFileExt_Kubernetes
from kg_prometheusstack import PrometheusStackBuilder, PrometheusStackOptions
from kg_traefik2 import Traefik2Builder, Traefik2Options, Traefik2OptionsPort
from kgpr_core.amazon.eks.kresource import KRPersistentVolumeProfile_AWSElasticBlockStore
from kgpr_core.amazon.eks.provider import ProviderAmazonEKS
from kgpr_core.digitalocean.kubernetes.kresource import KRPersistentVolumeProfile_CSI_DOBS
from kgpr_core.digitalocean.kubernetes.provider import ProviderDigitalOceanKubernetes
from kgpr_core.google.gke.kresource import KRPersistentVolumeProfile_GCEPersistentDisk
from kgpr_core.google.gke.provider import ProviderGoogleGKE
from kgpr_core.k3d.generic.provider import ProviderK3DGeneric
from kubragen import KubraGen
from kubragen.consts import PROVIDER_K3D, PROVIDER_GOOGLE, PROVIDER_DIGITALOCEAN, PROVIDER_AMAZON
from kubragen.helper import QuotedStr
from kubragen.jsonpatch import FilterJSONPatches_Apply, FilterJSONPatch
from kubragen.kresource import KRPersistentVolumeProfile_HostPath, KRPersistentVolumeClaimProfile_Basic
from kubragen.object import Object
from kubragen.option import OptionRoot
from kubragen.options import Options
from kubragen.output import OutputProject, OutputFile_ShellScript, OutputFile_Kubernetes, OD_FileTemplate, \
    OutputDriver_Directory


def main():
    parser = argparse.ArgumentParser(description='Kube Creator')
    parser.add_argument('-p', '--provider', help='provider', required=True, choices=[
        'google-gke',
        'amazon-eks',
        'digitalocean-kubernetes',
        'k3d',
    ])
    parser.add_argument('-o', '--output-path', help='output path', default='output')
    args = parser.parse_args()

    if args.provider == 'k3d':
        kgprovider = ProviderK3DGeneric()
    elif args.provider == 'google-gke':
        kgprovider = ProviderGoogleGKE()
    elif args.provider == 'digitalocean-kubernetes':
        kgprovider = ProviderDigitalOceanKubernetes()
    elif args.provider == 'amazon-eks':
        kgprovider = ProviderAmazonEKS()
    else:
        raise Exception('Unknown target')

    kg = KubraGen(provider=kgprovider, options=Options({
        'namespaces': {
            'default': 'default',
            'mon': 'monitoring',
        },
    }))

    if kgprovider.provider == PROVIDER_K3D:
        kg.resources().persistentvolumeprofile_add('default', KRPersistentVolumeProfile_HostPath())
    elif kgprovider.provider == PROVIDER_GOOGLE:
        kg.resources().persistentvolumeprofile_add('default', KRPersistentVolumeProfile_GCEPersistentDisk())
    elif kgprovider.provider == PROVIDER_DIGITALOCEAN:
        kg.resources().persistentvolumeprofile_add('default', KRPersistentVolumeProfile_CSI_DOBS())
    elif kgprovider.provider == PROVIDER_AMAZON:
        kg.resources().persistentvolumeprofile_add('default', KRPersistentVolumeProfile_AWSElasticBlockStore())

    if kgprovider.provider == PROVIDER_K3D:
        kg.resources().persistentvolumeclaimprofile_add('default', KRPersistentVolumeClaimProfile_Basic(allow_selector=False))
    else:
        kg.resources().persistentvolumeclaimprofile_add('default', KRPersistentVolumeClaimProfile_Basic())

    kg.resources().persistentvolume_add('prometheus-storage', 'default', {
        'hostPath': {
            'path': '/var/storage/prometheus'
        },
        'csi': {
            'fsType': 'ext4',
        },
    }, {
        'metadata': {
            'labels': {
                'pv.role': 'prometheus',
            },
        },
        'spec': {
            'persistentVolumeReclaimPolicy': 'Retain',
            'capacity': {
                'storage': '50Gi'
            },
            'accessModes': ['ReadWriteOnce'],
        },
    })

    kg.resources().persistentvolumeclaim_add('prometheus-storage-claim', 'default', {
        'namespace': 'monitoring',
        'persistentVolume': 'prometheus-storage',
    }, {
        'spec': {
            'selector': {
                'matchLabels': {
                    'pv.role': 'prometheus',
                }
            },
        }
    })

    out = OutputProject(kg)

    shell_script = OutputFile_ShellScript('create_{}.sh'.format(args.provider))
    out.append(shell_script)

    shell_script.append('set -e')

    #
    # Provider setup
    #
    if kgprovider.provider == PROVIDER_K3D:
        storage_directory = os.path.join(os.getcwd(), 'output', 'storage')
        if not os.path.exists(storage_directory):
            os.makedirs(storage_directory)
        shell_script.append(f'# k3d cluster create kgsample-prometheus-stack --port 5051:80@loadbalancer --port 5052:443@loadbalancer -v {storage_directory}:/var/storage')

    #
    # OUTPUTFILE: namespace.yaml
    #
    file = OutputFile_Kubernetes('namespace.yaml')
    file.append([{
        'apiVersion': 'v1',
        'kind': 'Namespace',
        'metadata': {
            'name': 'monitoring',
        },
    }])
    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUTFILE: storage.yaml
    #
    file = OutputFile_Kubernetes('storage.yaml')

    file.append(kg.persistentvolume_build())
    file.append(kg.persistentvolumeclaim_build())

    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # SETUP: Traefik 2
    #
    traefik2_config = Traefik2Builder(kubragen=kg, options=Traefik2Options({
            'namespace': OptionRoot('namespaces.default'),
            'config': {
                'traefik_args': [
                    '--api.dashboard=true',
                    '--api.insecure=false',
                    '--entrypoints.web.Address=:80',
                    '--entrypoints.api.Address=:8080',
                    '--entryPoints.metrics.address=:9090',
                    '--metrics.prometheus=true',
                    '--metrics.prometheus.entryPoint=metrics',
                    '--metrics.prometheus.addEntryPointsLabels=true',
                    '--providers.kubernetescrd',
                    f'--providers.kubernetescrd.namespaces=default,monitoring'
                ],
                'ports': [
                    Traefik2OptionsPort(name='web', port_container=80, port_service=80),
                    Traefik2OptionsPort(name='api', port_container=8080, port_service=8080),
                    Traefik2OptionsPort(name='metrics', port_container=9090, in_service=False),
                ],
                'create_traefik_crd': True,
                'prometheus_port': 9090,
                'prometheus_annotation': True,
            },
        })
    )

    traefik2_config.ensure_build_names(traefik2_config.BUILD_CRD, traefik2_config.BUILD_ACCESSCONTROL,
                                       traefik2_config.BUILD_SERVICE)

    #
    # OUTPUTFILE: traefik-config-crd.yaml
    #
    file = OutputFile_Kubernetes('traefik-config-crd.yaml')

    file.append(traefik2_config.build(traefik2_config.BUILD_CRD))

    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUTFILE: traefik-config.yaml
    #
    file = OutputFile_Kubernetes('traefik-config.yaml')

    file.append(traefik2_config.build(traefik2_config.BUILD_ACCESSCONTROL))

    file.append([{
        'apiVersion': 'traefik.containo.us/v1alpha1',
        'kind': 'IngressRoute',
        'metadata': {
            'name': 'traefik-api',
            'namespace': kg.option_get('namespaces.default'),
        },
        'spec': {
            'entryPoints': ['api'],
            'routes': [{
                'match': 'Method(`GET`)',
                'kind': 'Rule',
                'services': [{
                    'name': 'api@internal',
                    'kind': 'TraefikService'
                }]
            }]
        }
    }])

    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUTFILE: traefik.yaml
    #
    file = OutputFile_Kubernetes('traefik.yaml')

    file.append(traefik2_config.build(traefik2_config.BUILDITEM_SERVICE))

    file.append({
        'apiVersion': 'traefik.containo.us/v1alpha1',
        'kind': 'IngressRoute',
        'metadata': {
            'name': 'admin-traefik',
            'namespace': kg.option_get('namespaces.default'),
        },
        'spec': {
            'entryPoints': ['web'],
            'routes': [{
                'match': f'Host(`admin-traefik.localdomain`)',
                'kind': 'Rule',
                'services': [{
                    'name': traefik2_config.object_name('service'),
                    'port': 8080
                }],
            }]
        }
    })

    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # SETUP: prometheusstack
    #
    pstack_config = PrometheusStackBuilder(kubragen=kg, options=PrometheusStackOptions({
        'namespace': OptionRoot('namespaces.mon'),
        'config': {
            'prometheus_annotation': True,
            'prometheus_service_port': 80,
            'prometheus_config': PrometheusConfigFile(options=PrometheusConfigFileOptions({
                'scrape': {
                    'prometheus': {
                        'enabled': True,
                    }
                },
            }), extensions=[PrometheusConfigFileExt_Kubernetes(
                insecure_skip_verify=True, scrape_cadvisor=kgprovider.provider != PROVIDER_K3D)]),
            'grafana_service_port': 80,
            'grafana_provisioning': {
                'datasources': [{
                    'name': 'Prometheus',
                    'type': 'prometheus',
                    'access': 'proxy',
                    'url': 'http://{}:{}'.format('prometheus', 80),
                }],
                'dashboards': [
                    {
                        'name': 'default',
                        'type': 'file',
                    },
                ],
            },
            'grafana_dashboards': [
                GrafanaDashboardSource_GNet(provider='default', name='prometheus', gnetId=2, revision=2,
                                            datasource='Prometheus'),
                GrafanaDashboardSource_Url(provider='default', name='kubernetes',
                                           url='https://raw.githubusercontent.com/zaneclaes/grafana-dashboards/master/kubernetes.json'),
            ],
            'grafana_admin': {
                'user': 'myuser',
                'password': 'mypassword',
            },
        },
        'kubernetes': {
            'volumes': {
                'prometheus-data': {
                    'persistentVolumeClaim': {
                        'claimName': 'prometheus-storage-claim'
                    }
                }
            },
        },
    })).object_names_change({
        'prometheus-service': 'prometheus',
    })

    pstack_config.ensure_build_names(pstack_config.BUILD_ACCESSCONTROL, pstack_config.BUILD_CONFIG,
                                     pstack_config.BUILD_SERVICE)

    #
    # OUTPUTFILE: prometheus-config.yaml
    #
    file = OutputFile_Kubernetes('prometheus-config.yaml')
    out.append(file)

    file.append(pstack_config.build(pstack_config.BUILD_ACCESSCONTROL, pstack_config.BUILD_CONFIG))

    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUTFILE: prometheus.yaml
    #
    file = OutputFile_Kubernetes('prometheus.yaml')
    out.append(file)

    file.append(pstack_config.build(pstack_config.BUILD_SERVICE))

    file.append([{
        'apiVersion': 'traefik.containo.us/v1alpha1',
        'kind': 'IngressRoute',
        'metadata': {
            'name': 'admin-prometheus',
            'namespace': kg.option_get('namespaces.mon'),
        },
        'spec': {
            'entryPoints': ['web'],
            'routes': [{
                'match': f'Host(`admin-prometheus.localdomain`)',
                'kind': 'Rule',
                'services': [{
                    'name': pstack_config.object_name('prometheus-service'),
                    'namespace': pstack_config.namespace(),
                    'port': pstack_config.option_get('config.prometheus_service_port'),
                }],
            }]
        }
    }, {
        'apiVersion': 'traefik.containo.us/v1alpha1',
        'kind': 'IngressRoute',
        'metadata': {
            'name': 'admin-grafana',
            'namespace': kg.option_get('namespaces.mon'),
        },
        'spec': {
            'entryPoints': ['web'],
            'routes': [{
                'match': f'Host(`admin-grafana.localdomain`)',
                'kind': 'Rule',
                'services': [{
                    'name': pstack_config.object_name('grafana-service'),
                    'namespace': pstack_config.namespace(),
                    'port': pstack_config.option_get('config.grafana_service_port'),
                }],
            }]
        }
    }])

    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUTFILE: http-echo.yaml
    #
    file = OutputFile_Kubernetes('http-echo.yaml')
    out.append(file)

    file.append([{
        'apiVersion': 'apps/v1',
        'kind': 'Deployment',
        'metadata': {
            'name': 'echo-deployment',
            'namespace': kg.option_get('namespaces.default'),
            'labels': {
                'app': 'echo'
            }
        },
        'spec': {
            'replicas': 1,
            'selector': {
                'matchLabels': {
                    'app': 'echo'
                }
            },
            'template': {
                'metadata': {
                    'labels': {
                        'app': 'echo'
                    }
                },
                'spec': {
                    'containers': [{
                        'name': 'echo',
                        'image': 'mendhak/http-https-echo',
                        'ports': [{
                            'containerPort': 80
                        },
                        {
                            'containerPort': 443
                        }],
                    }]
                }
            }
        }
    },
    {
        'apiVersion': 'v1',
        'kind': 'Service',
        'metadata': {
            'name': 'echo-service',
            'namespace': kg.option_get('namespaces.default'),
        },
        'spec': {
            'selector': {
                'app': 'echo'
            },
            'ports': [{
                'name': 'http',
                'port': 80,
                'targetPort': 80,
                'protocol': 'TCP'
            }]
        }
    }, {
        'apiVersion': 'traefik.containo.us/v1alpha1',
        'kind': 'IngressRoute',
        'metadata': {
            'name': 'http-echo',
            'namespace': kg.option_get('namespaces.default'),
        },
        'spec': {
            'entryPoints': ['web'],
            'routes': [{
                # 'match': f'Host(`http-echo.localdomain`)',
                'match': f'PathPrefix(`/`)',
                'kind': 'Rule',
                'services': [{
                    'name': 'echo-service',
                    'port': 80,
                }],
            }]
        }
    }])

    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUTFILE: ingress.yaml
    #
    file = OutputFile_Kubernetes('ingress.yaml')
    http_path = '/'
    if kgprovider.provider == PROVIDER_GOOGLE or kgprovider.provider == PROVIDER_AMAZON:
        http_path = '/*'

    file_data = [
        Object({
            'apiVersion': 'extensions/v1beta1',
            'kind': 'Ingress',
            'metadata': {
                'name': 'ingress',
                'namespace': kg.option_get('namespaces.default'),
            },
            'spec': {
                'rules': [{
                    'http': {
                        'paths': [{
                            'path': http_path,
                            'backend': {
                                'serviceName': traefik2_config.object_name('service'),
                                'servicePort': 80,
                            }
                        }]
                    }
                }]
            }
        }, name='ingress', source='app', instance='ingress')
    ]

    if kgprovider.provider == PROVIDER_AMAZON:
        FilterJSONPatches_Apply(file_data, jsonpatches=[
            FilterJSONPatch(filters={'names': ['ingress']}, patches=[
                {'op': 'merge', 'path': '/metadata', 'value': {'annotations': {
                    'kubernetes.io/ingress.class': 'alb',
                    'alb.ingress.kubernetes.io/scheme': 'internet-facing',
                    'alb.ingress.kubernetes.io/listen-ports': QuotedStr('[{"HTTP": 80}]'),
                }}}
            ])
        ])

    file.append(file_data)
    out.append(file)
    shell_script.append(OD_FileTemplate(f'kubectl apply -f ${{FILE_{file.fileid}}}'))

    #
    # OUTPUT
    #
    output_path = os.path.join(args.output_path, '{}-{}'.format(
        args.provider, datetime.datetime.today().strftime("%Y%m%d-%H%M%S")))
    print('Saving files to {}'.format(output_path))
    if not os.path.exists(output_path):
        os.makedirs(output_path)

    out.output(OutputDriver_Directory(output_path))


if __name__ == "__main__":
    main()
