# KubraGen Sample: Prometheus Stack deployment

This sample deploys a Prometheus Stack (Prometheus, Grafana, Kube-State-Metrics and Node Exporter),
with a Traefik 2 edge router and a sample echo application simulating an application.

The echo application is the default at port 80, to access the other services
use these hosts in the ```hosts``` file:

* admin-traefik.localdomain: traefik dashboard
* admin-prometheus.localdomain: prometheus dashboard
* admin-grafana.localdomain: grafana dashboard
