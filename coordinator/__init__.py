from fastapi import FastAPI

app = FastAPI()

nodes = {}

@app.get("/")
def home():
    return {"message": "LowLlama coordinator running"}

@app.get("/nodes")
def get_nodes():
    return list(nodes.values())