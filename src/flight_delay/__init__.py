"""Flight delay prediction pipeline.

Top-level package. Subpackages map to pipeline stages:
    ingest   - API clients + pollers (raw data in)
    features - trajectory + weather feature logic
    training - dataset build, train, evaluate
    serving  - FastAPI app (predictions out)
    common   - config, schemas, storage IO shared across stages
"""

__version__ = "0.1.0"
