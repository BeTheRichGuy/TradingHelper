"""임포트 누락·오타를 정적으로 잡는다.

`th replay` 가 `NameError: name 'Text' is not defined` 로 죽은 적이 있다.
테스트 47개가 전부 통과한 상태였는데, 화면 코드는 아무 테스트도 실행하지
않았기 때문이다. 그런 종류는 **실행하지 않고도** 잡을 수 있다.

`symtable` 로 각 함수가 참조하는 이름 중 모듈 전역에도 빌트인에도 없는 것을 찾는다.
지역 변수·매개변수·클로저는 제외한다.
"""

from __future__ import annotations

import builtins
import pathlib
import symtable

# 파이썬이 모듈마다 자동으로 넣어주는 이름들
MODULE_GLOBALS = {"__file__", "__name__", "__doc__", "__package__", "__spec__",
                  "__loader__", "__builtins__", "__path__", "__debug__"}
BUILTINS = set(dir(builtins)) | MODULE_GLOBALS
ROOT = pathlib.Path(__file__).resolve().parent.parent


def python_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for base in ("src/th", "tests"):
        out += [
            p for p in (ROOT / base).rglob("*.py")
            if "__pycache__" not in p.parts
        ]
    return sorted(out)


def undefined_names(path: pathlib.Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    top = symtable.symtable(src, str(path), "exec")
    module_names = set(top.get_identifiers())
    found: list[str] = []

    def defined_at_module_scope(name: str) -> bool:
        """모듈 최상위에 실제로 만들어진 이름인가 (대입·임포트·def·class)."""
        if name not in module_names:
            return False
        sym = top.lookup(name)
        return sym.is_assigned() or sym.is_imported()

    def walk(table: symtable.SymbolTable, is_module: bool = False) -> None:
        for name in table.get_identifiers():
            sym = table.lookup(name)
            if name in BUILTINS:
                continue
            if is_module:
                # 모듈 최상위에서는 '지역'이라는 개념이 없다. 참조만 되고 어디서도
                # 만들어지지 않았으면 임포트 누락이다 (`@dataclass` 를 빠뜨린 적 있다).
                if sym.is_referenced() and not defined_at_module_scope(name):
                    found.append(f"모듈 최상위의 '{name}'")
                continue
            if sym.is_parameter() or sym.is_local():
                continue
            if sym.is_global() and not defined_at_module_scope(name):
                found.append(f"{table.get_name()}() 안의 '{name}'")
        for child in table.get_children():
            walk(child)

    walk(top, is_module=True)
    return found


def test_every_module_only_uses_names_it_has():
    problems: list[str] = []
    for path in python_files():
        for hit in undefined_names(path):
            problems.append(f"{path.relative_to(ROOT)}: {hit}")
    assert not problems, "정의되지 않은 이름:\n  " + "\n  ".join(problems)


def test_the_checker_catches_a_module_level_missing_import(tmp_path):
    """모듈 최상위 NameError 는 함수 안보다 더 흔하고 더 빨리 죽는다.

    `@dataclass` 임포트를 빠뜨린 적이 있는데, 예전 검사기는 최상위를 '지역'으로
    보고 통째로 건너뛰어 못 잡았다.
    """
    broken = tmp_path / "m.py"
    broken.write_text(
        "@dataclass\nclass A:\n    x: int = 0\n", encoding="utf-8"
    )
    assert any("dataclass" in x for x in undefined_names(broken))

    fixed = tmp_path / "m_ok.py"
    fixed.write_text(
        "from dataclasses import dataclass\n\n\n@dataclass\nclass A:\n    x: int = 0\n",
        encoding="utf-8",
    )
    assert undefined_names(fixed) == []


def test_every_module_actually_imports():
    """정적 검사가 놓치는 것을 실제 임포트가 잡는다. 두 겹으로 둔다."""
    import importlib

    failures = []
    for path in python_files():
        if path.parts[-2] == "tests" or path.name == "__init__.py":
            continue
        rel = path.relative_to(ROOT / "src") if "src" in path.parts else None
        if rel is None:
            continue
        module = ".".join(rel.with_suffix("").parts)
        try:
            importlib.import_module(module)
        except ImportError as e:                 # 선택 의존성이 없는 환경은 넘어간다
            if "No module named" in str(e) and "th." not in str(e):
                continue
            failures.append(f"{module}: {e}")
        except Exception as e:
            failures.append(f"{module}: {type(e).__name__}: {e}")
    assert not failures, "임포트 실패:\n  " + "\n  ".join(failures)


def test_the_checker_actually_catches_a_missing_import(tmp_path):
    """검사기가 조용히 아무것도 안 잡는 상태가 되면 이 테스트가 알려준다."""
    broken = tmp_path / "broken.py"
    broken.write_text(
        "def go():\n    return Text('안녕')\n", encoding="utf-8"
    )
    assert any("Text" in x for x in undefined_names(broken))

    fixed = tmp_path / "fixed.py"
    fixed.write_text(
        "from rich.text import Text\n\n\ndef go():\n    return Text('안녕')\n",
        encoding="utf-8",
    )
    assert undefined_names(fixed) == []
