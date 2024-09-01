# sat-perf-corellation

Satellite6 Performance Correlation finder

A tool to help finding the trigger of performance problems in Satellite6.

## Main idea

Enable PCP monitoring and Satellite telemetry to collect stats about CPU/RAM usage and namely potential triggers of high load. On collected PCP data, find highest correlations between potential triggers and system resources.

_**"What is correlation?"**_: it is a number specifying how dependant two events are. See e.g. [Wikipedia](https://en.wikipedia.org/wiki/Correlation). Roughly speaking, if there is correlation 0.75 between "duration of get me hosts lists requests" and CPU load, then there is 75% probability the durable hosts lists requests trigger the high CPU load.

### Requirements

- Satellite 6.13 or newer
  - An Ansible collection and few packages will be installed there
  - `foreman` source code modification is required (some constant will be enhanced)
- any system with `python3-pandas` package installed, to evaluate the collected PCP data

## How to install PCP monitoring

Install proper Ansible collection to the Satellite:

`ansible-galaxy collection install community.general -p /usr/share/ansible/collections/`

And run the playbook:

`ansible-playbook -e 'ansible_python_interpreter=/usr/bin/python3' sat6-perf-monitor.yaml`

Optionally run the playbook from any other system - just comment out `connection: local` and update `hosts: localhost` in the playbook accordingly.

The playbook will install and configure PCP, enable `statsd` telemetry in `foreman` and update list of API endpoints to gather PCP metrics for (this requires the source code change, the playbook does it for you; [Foreman upstream PR](https://github.com/theforeman/foreman/pull/10243) will move the config to settings).

### How to uninstall/disable the changes?

If you want to undo the changes, **do it carefully**. Esp. uninstalling some `pcp` packages can remove Satellite packages/installation. Rather just stop and disable relevant services:

```
systemctl stop pmcd pmlogger
systemctl disable pmcd pmlogger
```

and disbale Satellite Telemetry:

```
satellite-installer --foreman-telemetry-statsd-enabled false
```

## How to find correlations

Once the performance problem appears, identify the `pmlogger` file with collected stats, generate CSV from it and then run the Python script to find correlations:

- identify the right `pmlogger` file: on the Satellite system, it should be at `/var/log/pcp/pmlogger/HOSTNAME/DATE.TIME.0`, like `/var/log/pcp/pmlogger/pmoravec-sat615.gsslab.brq2.redhat.com/20240608.16.54.0`. Ensure the file covers the period of high load; to see time period covered by the file, run:

```
pmrep -p -t 60 -f "%Y-%m-%d %H:%M:%S" -a $pmfile mem.util.committed_AS | head
pmrep -p -t 60 -f "%Y-%m-%d %H:%M:%S" -a $pmfile mem.util.committed_AS | tail
```

- generate the CSV file there:

```
pmfile=$(pminfo -f pmcd.pmlogger.archive | grep -m1 value | awk -F \" '{ print $(NF-1)}')  # .. or some older one
stats=$(pminfo -a $pmfile | sort | grep -e statsd.fm_rails -e openmetrics.foreman_tasks -e openmetrics.dynflow_steps -e openmetrics.pulp_tasks | grep -v "\.$" | tr '\n' ' ')
pmrep -p -t 60 -J10 -o csv -a $pmfile kernel.all.load mem.util.committed_AS $stats > pmrep.statsd.csv
```

- run attached Python script on any system (not necessarily on the Satellite) that has `python3-pandas` package installed:

```
$ ./find_correlation_in_pcp_data.py --input-csv pmrep.statsd.csv --show-symptoms true
Correlations with TRIGGERS
    correlation vs. CPU:
        statsd.fm_rails_http_requests.api_v2_hosts_controller.index.200-/                               0.725793
        statsd.fm_rails_http_requests.katello_api_rhsm_candlepin_dynflow_proxy_controller.other.200-/   0.220671
        statsd.fm_rails_http_requests.katello_api_rhsm_candlepin_proxies_controller.other.204-/         0.220671
        statsd.fm_rails_http_requests.other.other.201-/                                                 0.110011
        statsd.fm_rails_http_requests.katello_api_rhsm_candlepin_proxies_controller.facts.200-/         0.104765

    correlation vs. memory:
        statsd.fm_rails_http_request_db_duration.other.other-/count                             0.878687
        statsd.fm_rails_http_request_view_duration.other.other-/count                           0.878687
        statsd.fm_rails_http_request_total_duration.other.other-/count                          0.878687
        statsd.fm_rails_http_request_total_duration.api_v2_hosts_controller.index-/count        0.860377
        statsd.fm_rails_http_request_view_duration.api_v2_hosts_controller.index-/count         0.860377


Correlations with SYMPTOMS
    correlation vs. CPU:
        statsd.fm_rails_ruby_gc_freed_objects.api_v2_hosts_controller.index-/           0.730118
        statsd.fm_rails_ruby_gc_allocated_objects.api_v2_hosts_controller.index-/       0.726014
        statsd.fm_rails_ruby_gc_minor_count.api_v2_hosts_controller.index-/             0.706371
        statsd.fm_rails_ruby_gc_count.api_v2_hosts_controller.index-/                   0.703729
        statsd.fm_rails_ruby_gc_major_count.api_v2_hosts_controller.index-/             0.688542

    correlation vs. memory:
        statsd.fm_rails_login_pwhash_duration.bcrypt-/count     0.850884
        statsd.fm_rails_login_pwhash_duration.pbkdf2sha1-/count 0.850884
        statsd.fm_rails_login_pwhash_duration.sha1-/count       0.850884
        statsd.fm_rails_login_pwhash_duration.bcrypt-/max       0.846479
        statsd.fm_rails_login_pwhash_duration.pbkdf2sha1-/max   0.764230


$
```

`TRIGGERS` means the behaviour behind the statistics can trigger / cause the high load. For example, `statsd.fm_rails_http_requests.api_v2_hosts_controller.index.200-/` as the "biggest CPU trigger" means "requests to list Hosts (that received 200 return code)" probably caused the high load.

`SYMPTOMS` means the behaviour behind the statistics is rather a symptom / side effect than a real trigger. Like `statsd.fm_rails_ruby_gc_freed_objects.api_v2_hosts_controller.index-/` means how much memory was freed during processing "get me Hosts" requests. Freeing memory itself is (most probably) not a sinner of the high CPU load, but a victim / side observation. It can point you to the root cause, though.

The numerical values are correlations, that range from -1 ("when I **increase** the trigger/symptom, load directly and proportionally **decreases**") via 0 ("there is absolutely no relation between the trigger/symptom and the load") to 1 ("when I **increase** the trigger/symptom, load directly and proportionally **increases**"). You can read a value like `0.725793` like "there is 72% chance that this does trigger the high load".