import argparse
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="Schema Service")

# Default schema directory, can be overridden via CLI args
config = {"schema_dir": "/data/schemas"}


@app.get("/{app_name}")
async def get_schema(app_name: str):
    """Return the JSON Schema for the given application name."""
    file_path = os.path.join(config["schema_dir"], f"{app_name}.schema.json")

    # Check if schema file exists
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail=f"Schema not found for app: {app_name}")

    try:
        with open(file_path, "r") as f:
            schema = json.load(f)
        return JSONResponse(content=schema)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    # Parse CLI arguments for flexible deployment
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema-dir", default="/data/schemas")
    parser.add_argument("--listen", default="0.0.0.0:5001")
    args = parser.parse_args()

    config["schema_dir"] = args.schema_dir
    host, port = args.listen.split(":")
    uvicorn.run(app, host=host, port=int(port))