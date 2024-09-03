#!/usr/bin/python3
# input data:
# pmfile=$(pminfo -f pmcd.pmlogger.archive | grep -m1 value | awk -F \" '{ print $(NF-1)}')  # .. or some older one  # noqa E501
# stats=$(pminfo -a $pmfile | sort | grep -e statsd.fm_rails -e openmetrics.foreman_tasks -e openmetrics.dynflow_steps -e openmetrics.pulp_tasks | grep -v "\.$" | tr '\n' ' ')  # noqa E501
# pmrep -p -t 60 -J10 -o csv -a $pmfile kernel.all.load mem.util.committed_AS proc.hog.cpu proc.hog.mem $stats > pmrep.input.csv  # noqa E501

import argparse
import pandas as pd

# how much to intend "nested" information
INDENT = " " * 4

# some columns/statistics are irelevant for finding trigger/symptom behind a
# correlation, drop them:
DROP_COLUMN_SUFFIXES = ["-/median",
                        "-/min",
                        "-/percentile90",
                        "-/percentile95",
                        "-/percentile99",
                        "-/std_deviation"]
DROP_COLUMN_SUFFIXES_RE = rf"({'|'.join(DROP_COLUMN_SUFFIXES)})$"

# ignore these columns for computing and sorting correlation - it makes little
# sense to correlate 1m load to 5m load or similar
DROP_COLUMNS_FOR_CORREL = ["kernel.all.load",
                           "mem.util",
                           "statsd.pmda"]
DROP_COLUMNS_FOR_CORREL_RE = rf"^({'|'.join(DROP_COLUMNS_FOR_CORREL)})"

# some columns are rather symptoms of a load than a trigger of the load itself
# split them to optional comparison of symptoms correlation
COLUMNS_FOR_TRIGGERS_PREFIXES = ["statsd.fm_rails_http_request",
                                 "statsd.fm_rails_importer_facts",
                                 "openmetrics.foreman_tasks"]
COLUMNS_FOR_TRIGGERS_PREFIXES_RE = (
    rf"^({'|'.join(COLUMNS_FOR_TRIGGERS_PREFIXES)})"
    )

# panda.Series strips leading blank characters of keys when printing whole
# Series which makes impossible to add indentation the best way is
# to implement own formatting of printing a Series


def print_df_indended(series, indent=INDENT*2):
    if series.empty:
        print(f"{indent}(no data available)")
        print()
        return
    maxwidth = max([len(s) for s in series.keys()])
    colwidth = (1 + maxwidth//8) * 8
    for k in series.keys():
        print(f"{indent}{k:<{colwidth}}{series[k]:.6f}")
    print()

# Below method modifies some input data so it deserves explanation.
#
# If we see duration value, say, 97 seconds appearing at time 12:32, then it
# affected CPU and RAM consumption not only in the last minute, but also in
# past. So we must increase past values of this metric/column. But how?
#
# First, how far into past we must go? We don't know at what second the
# long-durable event finished. It could finish at 12:31:01 or 12:31:34 or
# 12:31:58. So let approximate it by average value, 12:31:30. That means, this
# minute sample was affected by 30 seconds of that event (on average),
# previous minute by min(97-30,60)=60 seconds, and yet previous minute by
# min(97-60-60,60)=7 seconds. So we must modify times 12:30, 12:31 and 12:32.
#
# Second: why must modify also the current minute, 12:32? Because we know that
# processing the event took *just* 30 seconds in *this* minute, not 97 seconds.
# So it spun up CPU for 30 seconds in this minute.
#
# Hold on, what about memory? So far we were thinking CPU usage and assumed a
# long-running request consumes CPU in a constant manner over it's lifetime.
# But how it consumes memory? That is most increased at the end of the request
# processing. So how to deal with that? Well.. not ideally but in a
# similar manner like with CPU.
#
# First (for memory), we can't deal directly with memory consumption. The
# problem is there are (pulp+foreman) worker processes that respawn time to
# time with much lower memory consumption than the just-killed worker process.
# So overall mem.consumption can suddenly and randomly *drop* and that is a
# huge bias for correlation. How to minimalize this bias? The best I came with
# is to replace memory consumption by *increase* of the consumption. Was memory
# usage decreased in past minute? Then we know nothing what contributed to mem.
# increase, so deal with zero value. Was memory usage increased by 12MB in past
# minute? Then deal with this value.
#
# So now we will correlate durations with evident memory increase. And here, we
# assume processing a request consumes memory (so have impact to memory
# increase) constantly over its lifetime - likewise to CPU usage.
#
# So, for correlation with memory, we can modify input duration data the same
# way and just find correlation against memory increase (not memory
# consumption).
#
# TODO(Improve performance..? this takes a lot of time...)


def split_long_duration_to_past(my_df):
    for col in my_df.columns:
        if 'duration' not in col:
            continue
        for i in range(0, my_df[col].size):
            if my_df[col][i] > 30:
                remaining = my_df[col][i] - 30
                j = i-1
                while j >= 0 and remaining > 0:
                    my_df.at[j, col] += min(remaining, 60)
                    j -= 1
                    remaining -= 60
                my_df.at[i, col] = 30


def find_correl_in_df(df_load, my_df, correl_type, load_type):
    print(f"Correlations with {correl_type}")
    split_long_duration_to_past(my_df)
    if load_type != 'memory':
        print(f"{INDENT}correlation vs. CPU:")
        metric_corr_cpu = (
            my_df.corrwith(df_load["CPU"],
                           numeric_only=True)
            .fillna(value=0).sort_values(ascending=False)[:args.items_limit]
            )
        print_df_indended(metric_corr_cpu)
    if load_type != 'CPU':
        metric_corr_mem = (
            my_df.corrwith(df_load["memory"], numeric_only=True)
            .fillna(value=0).sort_values(ascending=False)[:args.items_limit]
            )
        print(f"{INDENT}correlation vs. memory:")
        print_df_indended(metric_corr_mem)
    print()


parser = argparse.ArgumentParser(description="PCP correlation finder")
parser.add_argument("--load-type",
                    choices=['CPU', 'memory', 'both'],
                    default='both',
                    help="What load types to find correlation? "
                         + "(CPU, mem, both)")
parser.add_argument("--input-csv",
                    required=True,
                    help="Input CSV file with PCP stats")
parser.add_argument("--items-limit",
                    type=int,
                    default=5,
                    help="Limit of correlation items per a metric")
parser.add_argument("--detailed",
                    action='store_true',
                    default=False,
                    help="Compare correlations to more detailed"
                         + " (less coarse) metrics (TODO)")
parser.add_argument("--show-symptoms",
                    action='store_true',
                    default=False,
                    help="Show also correlations to symptoms, "
                         + "not only triggers")
parser.add_argument("--show-load-stats",
                    action='store_true',
                    default=False,
                    help="Show statistics about the load (TODO)")
parser.add_argument("--peaks",
                    action='store_true',
                    default=False,
                    help="Show correlation only to peaks of CPU/mem usage "
                         + "(TODO)")
args = parser.parse_args()
infile = args.input_csv

headers = [*pd.read_csv(infile, nrows=1)]
df = pd.read_csv(infile, usecols=headers[1:]).fillna(value=0)

# ensure proper load-related metrics are present
if args.load_type != "CPU" and "mem.util.committed_AS" not in df.columns:
    print("Unable to see 'mem.util.committed_AS' metric in input data. "
          + " Please re-run with --load-type=CPU")
    exit(1)
if args.load_type != "memory" and "kernel.all.load-1 minute" not in df.columns:
    print("Unable to see 'kernel.all.load-1 minute' metric in input data. "
          + "Please re-run with --load-type=memory")
    exit(1)

# drop columns with /median, /min, /percentile or /std_deviation
# these are not important for finding triggers or symptoms
df.drop(
    list(df.filter(regex=DROP_COLUMN_SUFFIXES_RE)),
    axis=1,
    inplace=True
    )
# drop columns with zeroes only - they are irelevant for us
# and keeping them raises some CPython warning on execution
df = df.loc[:, (df != 0).any(axis=0)]
# split the DataFrame to potential triggers and symptoms
# and also load (CPU/mem.consumtion)
df_triggers = df.filter(regex=COLUMNS_FOR_TRIGGERS_PREFIXES_RE)
df_symptoms = (
    df.drop(columns=list(df.filter(regex=COLUMNS_FOR_TRIGGERS_PREFIXES_RE)))
    .drop(columns=list(df.filter(regex=DROP_COLUMNS_FOR_CORREL_RE)))
    )
df_load = (
    df.filter(items=["kernel.all.load-1 minute",
              "mem.util.committed_AS"])
    .rename(columns={"kernel.all.load-1 minute": "CPU",
            "mem.util.committed_AS": "memory"})
    )

# df_symptoms:
# aggregate "proc.hog.cpu *" by process name, and "proc.hog.mem *" the same
# TODO(aggregate and then consumption->increase, or vice versa?)
# TODO(how to do this in an elegant way using groupby method?)
proc_df = pd.DataFrame()
for pivot in ["proc.hog.cpu", "proc.hog.mem"]:
    pivot_df = df_symptoms.filter(regex=fr"^{pivot}")
    cols = pivot_df.columns
    df_symptoms.drop(columns=cols, inplace=True)
    for group in list(set(cols.map(lambda x: x.split()[-1]))):
        proc_df[f"{pivot}:{group}"] = (pivot_df.filter(regex=fr'{group}')
                                       .sum(axis=1))
df_symptoms = df_symptoms.join(proc_df)

# modify memory consumption to memory increase.
# see explanation before split_long_duration_to_past method
# we must update the values from newest to oldest as for updating i-th value
# we need the original (i-1)th
# TODO(DO THE SAME WITH proc.hog.mem.* stats?)
if args.load_type != "CPU":  # modify the column only when necessary
    df_load.at[0, "memory"] = 0
    for i in range(df_load["memory"].size-1, 0, -1):
        oldest = df_load["memory"][i-1]
        newest = df_load["memory"][i]
        if oldest == 0 or oldest > newest:
            df_load.at[i, "memory"] = 0
        else:
            df_load.at[i, "memory"] = newest-oldest

find_correl_in_df(df_load, df_triggers, "TRIGGERS", args.load_type)
if args.show_symptoms:
    find_correl_in_df(df_load, df_symptoms, "SYMPTOMS", args.load_type)
