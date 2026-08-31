import json
from pathlib import Path
from dotenv import load_dotenv
from utils.wfInfo import getWfDependencies, getWfUpdYaml
import re


DATA_DIR = Path(__file__).resolve().parent.parent / "data"

ADH1_LIST_PATH = DATA_DIR / "wf_list_adh1.txt"
ADH3_LIST_PATH = DATA_DIR / "wf_list_adh3.txt"

# И открывайте их по этим путям:
with open(ADH1_LIST_PATH, "r", encoding="utf-8") as f:
    list_wf_adh1 = f.read().splitlines()

with open(ADH3_LIST_PATH, "r", encoding="utf-8") as f:
    list_wf_adh3 = f.read().splitlines()


def export_cluster_dependencies(flow_names, output_path):
    result = []

    for flow_name in flow_names:
        flow_record = build_flow_record(flow_name)
        result.append(flow_record)

    Path(output_path).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_flow_record(flow_name):
    raw_response = getWfDependencies(flow_name)
    conditions = json.loads(raw_response)["conditions"]

    return {
        "flowName": flow_name,
        "conditions": conditions,
    }


def remove_tasks_block(yaml_text: str) -> str:
    """
    Удаляет из YAML всё, начиная с верхнеуровневого для body блока `tasks:`.
    То есть ищет строку вида: два пробела + tasks:
    """

    match = re.search(r"(?m)^  tasks\s*:", yaml_text)

    if not match:
        raise ValueError(f"Блок `  tasks:` не найден для {yaml_text}")

    return yaml_text[:match.start()].rstrip() + "\n"


if __name__ == "__main__":
    load_dotenv()

    DATA_DIR.mkdir(exist_ok=True)

    list_wf_adh3_dep_upd = []

    # Используем правильные пути через DATA_DIR
    with open(ADH1_LIST_PATH, "r", encoding="utf-8") as f:
        list_wf_adh1 = f.read().splitlines()

    with open(ADH3_LIST_PATH, "r", encoding="utf-8") as f:
        list_wf_adh3 = f.read().splitlines()

    for i in list_wf_adh3:
        list_wf_adh3_dep_upd.append(remove_tasks_block(getWfUpdYaml(i)))

    yaml_output_path = DATA_DIR / "wf_adh3_dep.yaml"
    with open(yaml_output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(list_wf_adh3_dep_upd))

    export_cluster_dependencies(
        flow_names=list_wf_adh1,
        output_path=DATA_DIR / "adh1_new.json",
    )

    export_cluster_dependencies(
        flow_names=list_wf_adh3,
        output_path=DATA_DIR / "adh3_new.json",
    )