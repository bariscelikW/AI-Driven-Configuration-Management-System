import argparse
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

app = FastAPI(title="Values Service")

# Default values directory, can be overridden via CLI args
config = {"values_dir": "/data/values"}


@app.get("/{app_name}")
async def get_values(app_name: str):
    """Return the current configuration values for the given application name."""
    file_path = os.path.join(config["values_dir"], f"{app_name}.value.json")

    # Check if values file exists
    if not os.path.isfile(file_path):
        raise HTTPException(status_code=404, detail=f"Values not found for app: {app_name}")

    try:
        with open(file_path, "r") as f:
            values = json.load(f)
        return JSONResponse(content=values)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    # Parse CLI arguments for flexible deployment
    parser = argparse.ArgumentParser()
    parser.add_argument("--values-dir", default="/data/values")
    parser.add_argument("--listen", default="0.0.0.0:5002")
    args = parser.parse_args()

    config["values_dir"] = args.values_dir
    host, port = args.listen.split(":")
    uvicorn.run(app, host=host, port=int(port))