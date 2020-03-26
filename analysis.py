import argparse
import csv
import gzip
import json
import os
from argparse import Namespace
from datetime import datetime, timedelta
from glob import glob
from itertools import groupby
from typing import Any, Dict, List, NamedTuple, Tuple

import numpy as np
import plotly.figure_factory as ff
import plotly.graph_objs as go
import plotly.offline as py
import yaml
from jinja2 import Environment, FileSystemLoader

DEFAULT_GANTT_FILENAME = "gantt-overview.html"
DEFAULT_CSV_FILENAME = "durations.csv"
DEFAULT_STATISTICS_FILENAME = "statistics.html"
DEFAULT_RAW_STATS_FILENAME = "raw_stats.json"


LOGGER_DATE_FMT = "%Y-%m-%d %H:%M:%S.%f"


class Content(NamedTuple):
    timestamp: Any
    event: Any
    json: Dict[Any, Any]


class CSVRow(NamedTuple):
    num: int
    task_type: str
    duration: float
    nodes_involved: int


def read_raw_content(input_file: str) -> Tuple[List[Content], str]:
    content: List[str] = []

    if input_file.endswith("gz"):
        with gzip.open(input_file, "r") as f:
            content = [line.strip().decode() for line in f.readlines()]
    else:
        with open(input_file, "r") as f:
            content = [line.strip() for line in f.readlines()]

    stripped_content = []
    for row in content:
        if "run_number" in row:
            run_number = json.loads(row)["run_number"]
            continue
        x = json.loads(row)
        if "runtime" in x:
            stripped_content.append(Content(x["timestamp"], x["event"], x))

    # sort by timestamp
    stripped_content.sort(key=lambda e: e[0])

    return stripped_content, f"node_{run_number}_*/*.log*"


def draw_gantt(
    output_directory: str, filled_rows: Dict[str, List[Any]], summary: List[Dict[str, Any]]
) -> None:
    fig = ff.create_gantt(
        filled_rows["gantt_rows"],
        title="Raiden Analysis",
        show_colorbar=False,
        bar_width=0.5,
        showgrid_x=True,
        showgrid_y=True,
        height=928,
        width=1680,
    )

    fig["layout"].update(
        yaxis={
            # 'showticklabels':False
            "automargin": True
        },
        hoverlabel={"align": "left"},
    )

    div = py.offline.plot(fig, output_type="div")

    j2_env = Environment(
        loader=FileSystemLoader(os.path.dirname(os.path.abspath(__file__))), trim_blocks=True
    )
    output_content = j2_env.get_template("chart_template.html").render(
        gantt_div=div, task_table=filled_rows["table_rows"]
    )

    with open(f"{output_directory}/{DEFAULT_GANTT_FILENAME}", "w") as text_file:
        text_file.write(output_content)


def write_csv(output_directory: str, filled_rows: Dict[str, List[Any]]) -> None:
    with open(f"{output_directory}/{DEFAULT_CSV_FILENAME}", "w", newline="") as csv_file:
        csv_writer = csv.writer(csv_file, delimiter=",", quotechar="|", quoting=csv.QUOTE_MINIMAL)
        csv_writer.writerow(["Id", "Type", "Duration"])
        for r in filled_rows["csv_rows"]:
            csv_writer.writerow(r)


def generate_statistics(filled_rows: Dict[str, List[Any]]) -> List[Dict[str, Any]]:
    group_by_result: List[Dict[str, Any]] = []
    k = lambda r: r.task_type
    for key, group in groupby(sorted(filled_rows["csv_rows"], key=k), key=k):
        result = {}
        duration_transfers = list(map(lambda r: r.duration, list(group)))
        data = np.array(duration_transfers)
        result["raw_durations"] = duration_transfers
        result["name"] = key
        result["min"] = data.min()
        result["max"] = data.max()
        result["mean"] = data.mean()
        result["median"] = np.median(data)
        result["p95"] = np.percentile(a=data, q=95)
        result["stdev"] = data.std()
        result["count"] = data.size
        group_by_result.append(result)
        print

    return group_by_result


def write_statistics(output_directory: str, summary: List[Dict[str, Any]]) -> None:
    raw_stats: List[Dict[str, Any]] = []
    for num, result in enumerate(summary):
        data_array = np.array((result["raw_durations"]))
        histogram = go.Histogram(x=data_array, opacity=0.75)
        layout = go.Layout(
            barmode="overlay",
            width=500,
            height=300,
            margin=go.layout.Margin(l=50, r=50, b=50, t=0, pad=4),  # noqa: E741
        )
        fig = go.Figure(data=[histogram], layout=layout)
        div = py.offline.plot(
            fig, output_type="div", config={"displayModeBar": False}, include_plotlyjs=num == 0
        )
        result["div"] = div
        raw_stats.append(
            {
                key: f"{val}"
                for key, val in result.items()
                if key not in "raw_durations div".split()
            }
        )

    j2_env = Environment(
        loader=FileSystemLoader(os.path.dirname(os.path.abspath(__file__))), trim_blocks=True
    )
    output_content = j2_env.get_template("summary_template.html").render(summary=summary)

    with open(f"{output_directory}/{DEFAULT_STATISTICS_FILENAME}", "w") as text_file:
        text_file.write(output_content)

    with open(f"{output_directory}/{DEFAULT_RAW_STATS_FILENAME}", "w") as f:
        json.dump(raw_stats, f, indent=2)


def open_node_logs(node_log_glob: str) -> List[str]:
    node_logs = []
    for fn in glob(node_log_glob):
        if fn.endswith("gz"):
            with gzip.open(fn, "r") as file:
                node_logs.append(file.read().decode())
        else:
            with open(fn, "r") as file:
                node_logs.append(file.read())
    return node_logs


def count_log_occurrences(key: str, node_logs: List[str]) -> int:
    log_occurrences = 0
    for logfile in node_logs:
        if key in logfile:
            log_occurrences += 1
    return log_occurrences


def fill_rows(content: List[Content], node_logs: List[str]) -> Dict[str, List[Any]]:
    filled_rows: Dict[str, Any] = dict()
    gantt_rows: List[Dict[str, Any]] = []
    csv_rows: List[CSVRow] = []
    table_rows: List[Dict[str, Any]] = []

    for num, task in enumerate(content):
        task_body = task.json["task"].split(":", 1)
        task_type = task_body[0].replace("<", "").strip()
        task_desc = task_body[1].replace(">", "").strip()
        task_body_json = yaml.safe_load(task_desc.replace("'", '"'))

        # Skip WaitTask
        if not isinstance(task_body_json, dict):
            continue
        duration = task.json["runtime"]
        if "id" in task.json:
            num = task.json["id"]
        nodes_involved = 0
        if "identifier" in task_body_json:
            nodes_involved = count_log_occurrences(str(task_body_json["identifier"]), node_logs)

        if nodes_involved:
            task_type = f"{task_type}({nodes_involved} hops)"

        # add main task to rows
        task_body_json["nodes_involved"] = nodes_involved
        task_full_desc = json.dumps(task_body_json, sort_keys=True, indent=4).replace("\n", "<br>")
        gantt_rows.append(
            {
                "Task": f"{task_type}(#{num})",
                "Start": datetime.strftime(
                    datetime.strptime(task.timestamp, LOGGER_DATE_FMT)
                    - timedelta(seconds=duration),
                    LOGGER_DATE_FMT,
                ),
                "Finish": task.timestamp,
                "Description": task_full_desc,
            }
        )
        table_rows.append(
            {"id": num, "type": task_type, "duration": duration, "description": task_full_desc}
        )
        csv_rows.append(CSVRow(num, task_type, duration, nodes_involved))
        main_task_debug_string = f"{task_type}(#{num}): {task_desc}"
        print(main_task_debug_string)
        print(f"------------------------{duration}-------------------------------------")

    filled_rows["gantt_rows"] = gantt_rows
    filled_rows["csv_rows"] = csv_rows
    filled_rows["table_rows"] = table_rows
    return filled_rows


def parse_args() -> Namespace:
    parser = argparse.ArgumentParser(description="Raiden Scenario-Player Analysis")
    parser.add_argument(
        "input_file", nargs="*", help="File name of scenario-player log file as main input",
    )
    parser.add_argument(
        "--output-directory",
        type=str,
        dest="output_directory",
        default="raiden-scenario-player-analysis",
        help="Output directory name",
    )

    args = parser.parse_args()

    return args


def main() -> None:
    args = parse_args()

    stripped_content, nodes_glob = read_raw_content(args.input_file[0])

    log_path = os.path.dirname(args.input_file[0])

    node_logs = open_node_logs(os.path.join(log_path, nodes_glob))
    filled_rows = fill_rows(stripped_content, node_logs)
    summary = generate_statistics(filled_rows)

    if not os.path.exists(args.output_directory):
        os.makedirs(args.output_directory)

    draw_gantt(args.output_directory, filled_rows, summary)
    write_csv(args.output_directory, filled_rows)
    write_statistics(args.output_directory, summary)


if __name__ == "__main__":
    main()
