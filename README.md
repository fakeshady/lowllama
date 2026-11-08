
# LowLlama

**Run an AI model with help from multiple laptops.**

LowLlama connects Windows laptops over a private Tailscale network. One **master** laptop runs the AI model using an NVIDIA GPU, while **helper** laptops contribute CPU processing power.

A simple browser dashboard lets you connect devices, start the model, and chat with it.

Built for UB Hacking using Python, FastAPI, and llama.cpp.

## Quick Setup (Windows)

### Requirements

- [Git](https://git-scm.com/downloads)
- [Python 3.12](https://www.python.org/downloads/)
- [Tailscale](https://tailscale.com/download)
- NVIDIA GPU (master laptop only)
- Windows 10 or 11

### Step 1: Connect laptops

Install Tailscale on every laptop and make sure everyone is connected to the **same Tailscale network (tailnet)**.

### Step 2: Install LowLlama

On every laptop, open PowerShell and run:

```powershell
git clone https://github.com/fakeshady/lowllama.git
cd lowllama
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Step 3: Launch the dashboard

On each laptop, run:

```powershell
.\.venv\Scripts\python.exe ui\server.py
```

Open the dashboard:

**http://127.0.0.1:8600**

### Step 4: Set up the master

On the laptop with an NVIDIA GPU:

1. Select **Master** in the dashboard.
2. Follow the setup wizard.
3. Download the required AI model and runtime.
4. Start the coordinator.
5. Generate an invite code for helpers.

### Step 5: Connect helpers

On the other laptops:

1. Select **Helper** in the dashboard.
2. Install the helper runtime using the wizard.
3. Paste the master's invite code.
4. Join the network.

Helpers do not need an NVIDIA GPU or a copy of the AI model.

### Step 6: Start chatting

On the master's dashboard:

1. Select the connected helper laptops.
2. Start the AI model.
3. Open the chat interface and send a message.

## Troubleshooting

**Windows Firewall can sometimes block connections even when Tailscale is connected.**

If laptops cannot communicate:

- Make sure all devices are connected to the same Tailscale network.
- Check that the master coordinator is running.
- Ensure the master accepts connections on **TCP port 8000**.
- Ensure helpers accept connections on **TCP port 50052**.
- Check Windows Firewall settings and allow the required connections between trusted Tailscale devices.

**Do not disable Windows Firewall or expose these ports to the public internet.**

For more detailed troubleshooting, see the [Engine Setup and Operations Guide](docs/ENGINE_RUNTIME_AND_AGENT_OPERATIONS.md).

---

**Note:** LowLlama is an experimental hackathon prototype. Distributed inference depends on compatible hardware and a working network.
