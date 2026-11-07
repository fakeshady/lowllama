# LowLlama

## Run the coordinator (Windows PowerShell)

Create a virtual environment and install the dependencies from the project root:

```powershell
uv venv .venv --python 3.12
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
```

Start the FastAPI server:

```powershell
.\.venv\Scripts\python.exe -m uvicorn coordinator:app --reload
```

Open http://127.0.0.1:8000 for the coordinator or http://127.0.0.1:8000/docs
for the interactive API documentation. Stop the server with Ctrl+C.

The application is defined in `coordinator/__init__.py`. Running that file
directly does not start a server; use the Uvicorn command above.

In your IDE, select `.venv\Scripts\python.exe` as the project interpreter so
it can resolve the installed packages. For an IDE run configuration, run the
module `uvicorn` with parameters `coordinator:app --reload` and set the working
directory to the project root.
