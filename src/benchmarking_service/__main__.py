import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "benchmarking_service.app:app",
        host=os.environ.get("SERVICE_HOST", "0.0.0.0"),
        port=int(os.environ.get("SERVICE_PORT", "8080")),
    )


if __name__ == "__main__":
    main()
