"""传动设备本地化设计闭环命令行冒烟入口。"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from local_design import LocalizationCase


def main() -> None:
    item = LocalizationCase(**{'case_code': 'case-code-001', 'product_line': 'product-line-001', 'customer': 'customer-001', 'revision': 1})
    print(json.dumps({"item": asdict(item), "fingerprint": item.fingerprint()}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
