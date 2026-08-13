"""Scratch pad for poking at the ingest client by hand.

Run from the repo ROOT, as a module - not as a file path:

    uv run python -m tests.experiment

`python tests/experiment.py` puts sys.path[0] at tests/, so the `tests`
package itself becomes unimportable and `import tests.…` fails.
"""
from pathlib import Path
import polars as pl
import os


def main() -> None:
    print("Current Working Directory:", os.getcwd())
    script_dir = Path(__file__).parent

    path =  f"data/bronze/opensky_states/date=2026-08-07/hour=10/20260807T104823657745-b74fa419.parquet"
    df = pl.read_parquet(path)
    # df.collect_schema()
    # print(df.collect_schema())



if __name__ == "__main__":
    main()
