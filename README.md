LowLlama
Run an AI model with help from multiple laptops.

LowLlama connects Windows laptops over a private Tailscale network. One master laptop runs the AI model using an NVIDIA GPU, while helper laptops contribute CPU processing power. A browser dashboard helps you connect devices, start the model, and chat.

Built for UB Hacking with Python, FastAPI, and llama.cpp.
Setup (Windows)
You'll need Git, Python 3.12, Tailscale, and an NVIDIA GPU on the master laptop. Helpers do not need an NVIDIA GPU.
1. Install Tailscale on every laptop and connect everyone to the same tailnet.
2. On each laptop, open PowerShell and run:
   git clone https://github.com/fakeshady/lowllama.git
   cd lowllama
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe ui\server.py
3. Open http://127.0.0.1:8600 in a browser on each laptop.
4. On the NVIDIA laptop, choose Master and follow the setup wizard to download the model and start the coordinator. Share the generated invite code.
5. On the other laptops, choose Helper, download the helper runtime through the wizard, and join with the invite code.
6. Back on the master dashboard, select the connected helpers, start the model, and chat.
   The model downloads to the master during setup; helpers don't need to download it.
   Connection issues?
   Windows Firewall can block connections even when Tailscale is connected. If a helper cannot join, confirm the coordinator is running and all devices share the same tailnet. The master must accept connections on TCP 8000 and helpers on TCP 50052. Allow only required traffic between trusted Tailscale devices; do not disable the firewall or expose these ports publicly.

For detailed troubleshooting, see [engine setup and operations](docs/ENGINE_RUNTIME_AND_AGENT_OPERATIONS.md).
LowLlama is an experimental hackathon prototype. Multi-laptop inference depends on compatible hardware and a working network.