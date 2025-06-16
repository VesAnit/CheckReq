import modal
import os
import json
import subprocess
import logging
import sys
from datetime import datetime
import warnings
import requests
import time
import uuid
import fcntl
import re
from select import select
from fastapi.responses import StreamingResponse
import asyncio
from anthropic import Anthropic, APIError

warnings.filterwarnings("ignore", category=DeprecationWarning, module="langchain")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

app = modal.App("checkreq-modal", secrets=[modal.Secret.from_name("anthropic-secret")])

image_311 = modal.Image.debian_slim(python_version="3.11").run_commands(
    "apt-get update && apt-get install -y python3 python3-venv wget gnupg bzip2"
).pip_install(
    "anthropic==0.28.0", "httpx==0.27.0", "requests==2.32.3", "langchain==0.2.16", "fastapi[standard]==0.115.0"
)
image_312 = (
    modal.Image.from_registry("python:3.12-slim")
    .run_commands(
        "apt-get update && apt-get install -y wget gnupg bzip2"
    )
    .pip_install(
        "anthropic==0.28.0", "httpx==0.27.0", "requests==2.32.3", "langchain==0.2.16", "fastapi[standard]==0.115.0"
    )
)

SYSTEM_PROMPT_CYCLE1 = r"""
/think
Think step-by-step, using your internal knowledge to select compatible package versions. Reason through potential conflicts and explain your choices.
Return only a JSON object: {"response_type": "json", "bash_commands": "...", "test_script": "...", "reasoning_content": "<p>...</p>", "message": "..."}.
The response must be valid JSON without any surrounding text.
**Cycle 1: Generate Installation Commands and Test Script**
- Observation: User query with parameters (e.g., query: "Create an environment for web development", use_conda: True, use_gpu: False, version_preference: "stable", python_version: "3.12").
- Thought: 
  1. If "use_conda": True, use Micromamba at /volume/micromamba/micromamba. Identify the domain (e.g., correct "web dev" to "web development"). If query is unclear, return an error. Select a comprehensive set of packages for web development, ensuring compatibility with the requested python_version. Always include 'pip' in the package list to ensure it is available for subsequent pip installations and test scripts. For web development, include packages like flask==3.0.3, fastapi==0.115.0, sqlalchemy==2.0.35, uvicorn==0.32.0, gunicorn==23.0.0, requests==2.32.3, jinja2==3.1.4, pydantic==2.9.2, httpx==0.27.2, psycopg2-binary==2.9.10 for database connectivity. Specify exact package versions to ensure compatibility, determined by reasoning through dependencies and potential conflicts. Prioritize channels: 1) conda-forge, 2) defaults with --no-channel-priority. Generate commands to create an environment, installing core packages via Micromamba and additional packages via pip if unavailable in Conda channels. If use_gpu=True, include GPU-specific packages (e.g., pytorch==2.4.1 with cuda-toolkit=12.4) from conda-forge or pip with --extra-index-url https://download.pytorch.org/whl/cu124. If "version_preference": "stable", prefer stable, well-tested package versions; if "latest", prefer latest compatible versions. Do not install drivers. Generate test_script to verify all packages (e.g., import <package>; print(<package>.__version__);) and include GPU checks if relevant (e.g., import torch; print(torch.cuda.is_available())). 
  2. If "use_conda": False, use pip in a virtual environment. Identify the domain. If query is unclear, return an error. Select stable and compatible package versions, specifying versions for all packages (e.g., flask==3.0.3, fastapi==0.115.0). Generate commands for installation, ensuring pip is upgraded after venv creation. Include --extra-index-url https://download.pytorch.org/whl/cu124 only if use_gpu=True and GPU packages are required. Do not install drivers. Generate test_script to verify all packages and include GPU checks if relevant. 
- Action: Generate bash_commands for installation and test_script for verification. 
  For use_conda=True: use 
    mkdir -p /volume/workdir\n
    cd /volume/workdir\n
    /volume/micromamba/micromamba create -y -n env python={{python_version}} pip -c conda-forge --no-channel-priority\n
    /volume/micromamba/micromamba install -y -n env <conda_packages> -c conda-forge -c defaults --no-channel-priority\n
  If pip packages are needed (e.g., for unavailable Conda packages or GPU support), append:
    /volume/micromamba/micromamba run -n env pip install --no-cache-dir <pip_packages>\n
  If use_gpu=True, append --extra-index-url https://download.pytorch.org/whl/cu{{cuda_version_no_dots}} to pip install.
  For use_conda=False: use 
    mkdir -p /volume/workdir\n
    cd /volume/workdir\n
    python{{python_version}} -m venv venv\n
    source venv/bin/activate && pip install --upgrade pip && pip install --no-cache-dir <packages>\n
  If use_gpu=True, append --extra-index-url https://download.pytorch.org/whl/cu{{cuda_version_no_dots}} to pip install.
  Replace {{python_version}} with the user-specified python_version (e.g., 3.12), {{python_version_no_dots}} with Python version without dots (e.g., 312), and {{cuda_version_no_dots}} with CUDA version without dots (e.g., 124 for 12.4). Set test_script with import <package>; print(<package>.__version__); for each package, plus pip check, using semicolons. Include <p>...</p> in reasoning_content explaining package selection, version choices, channel priority, pip usage, and test script rationale. If uncertain about package availability, note in reasoning_content: "<p>Assuming package availability (e.g., flask); to be verified in Cycle 2.</p>". For invalid queries, return {"response_type": "json", "bash_commands": "", "test_script": "", "reasoning_content": "<p>Invalid query</p>", "message": "Please, specify your request"}.
  Never test jupyter notebook (just install it). Don't use commands import jupyter;print('jupyter:',jupyter.__version__) or any test commands for jupyter, it's compatible.
Example for conda: {"response_type": "json", "bash_commands": "mkdir -p /volume/workdir\ncd /volume/workdir\n/volume/micromamba/micromamba create -y -n env python=3.12 pip -c conda-forge --no-channel-priority\n/volume/micromamba/micromamba install -y -n env flask==3.0.3 fastapi==0.115.0 sqlalchemy==2.0.35 uvicorn==0.32.0 gunicorn==23.0.0 requests==2.32.3 jinja2==3.1.4 pydantic==2.9.2 httpx==0.27.2 -c conda-forge -c defaults --no-channel-priority\n/volume/micromamba/micromamba run -n env pip install --no-cache-dir psycopg2-binary==2.9.10\n", "test_script": "import flask;print('flask:',flask.__version__);import fastapi;print('fastapi:',fastapi.__version__);import sqlalchemy;print('sqlalchemy:',sqlalchemy.__version__);import uvicorn;print('uvicorn:',uvicorn.__version__);import gunicorn;print('gunicorn:',gunicorn.__version__);import requests;print('requests:',requests.__version__);import jinja2;print('jinja2:',jinja2.__version__);import pydantic;print('pydantic:',pydantic.__version__);import httpx;print('httpx:',httpx.__version__);import psycopg2;print('psycopg2:',psycopg2.__version__);import subprocess;print(subprocess.run(['pip', 'check'], capture_output=True, text=True).stdout)", "reasoning_content": "<p>Reasoned through web development package selection, choosing specific versions to ensure compatibility. Prioritized conda-forge for Conda packages, used pip for psycopg2-binary unavailable in Conda. Assuming package availability; to be verified in Cycle 2.</p>", "message": ""}
Example for venv: {"response_type": "json", "bash_commands": "mkdir -p /volume/workdir\ncd /volume/workdir\n/python3.12 -m venv venv\nsource venv/bin/activate && pip install --upgrade pip && pip install --no-cache-dir flask==3.0.3 fastapi==0.115.0 sqlalchemy==2.0.35 uvicorn==0.32.0 gunicorn==23.0.0 requests==2.32.3 jinja2==3.1.4 pydantic==2.9.2 httpx==0.27.2 psycopg2-binary==2.9.10", "test_script": "import flask;print('flask:',flask.__version__);import fastapi;print('fastapi:',fastapi.__version__);import sqlalchemy;print('sqlalchemy:',sqlalchemy.__version__);import uvicorn;print('uvicorn:',uvicorn.__version__);import gunicorn;print('gunicorn:',gunicorn.__version__);import requests;print('requests:',requests.__version__);import jinja2;print('jinja2:',jinja2.__version__);import pydantic;print('pydantic:',pydantic.__version__);import httpx;print('httpx:',httpx.__version__);import psycopg2;print('psycopg2:',psycopg2.__version__);import subprocess;print(subprocess.run(['pip', 'check'], capture_output=True, text=True).stdout)", "reasoning_content": "<p>Observation: User requested a web development environment without conda. Thought: Selected stable versions for pip installation, reasoning through compatibility. Action: Generated installation commands and test script to verify packages.</p>", "message": ""}
"""

SYSTEM_PROMPT_CYCLE2 = r"""
/think
Think step-by-step, using your internal knowledge to select compatible package versions. Reason through potential conflicts and explain your choices.
Return only a JSON object: {"response_type": "json", "bash_commands": "...", "test_script": "...", "reasoning_content": "<p>...</p>", "message": "..."}.
The response must be valid JSON without any surrounding text.
**Cycle 2: Execute Installation and Collect Logs**
- Observation: Receive JSON response from Cycle 1 containing bash_commands, test_script, reasoning_content, and message, along with User input JSON specifying the query and environment parameters (e.g., query, use_conda, use_gpu, version_preference, python_version).
- Thought: 
  1. If use_conda=True: Use Micromamba at /volume/micromamba/micromamba to execute the bash_commands from Cycle 1. Reason through package versions to ensure compatibility across conda-forge and defaults channels. Return the bash_commands from Cycle 1 and the same test_script if the installation succeeds. If the installation fails (e.g., package not found, version conflict), analyze errors and suggest fixes for Cycle 3, such as adjusting versions or using pip fallback. If flexible channel priority fails, try reordering channels (conda-forge, defaults). If use_gpu=True and GPU packages fail, note pip fallback with --extra-index-url https://download.pytorch.org/whl/cu{{cuda_version_no_dots}}. Do not install drivers.
  2. If use_conda=False: Execute the bash_commands from Cycle 1. Reason through package versions to ensure compatibility. Return the bash_commands from Cycle 1 and the same test_script if the installation succeeds. If the installation fails (e.g., version conflict), analyze errors and suggest fixes for Cycle 3, such as adjusting versions. Ensure pip is upgraded after venv creation. Include --extra-index-url https://download.pytorch.org/whl/cu{{cuda_version_no_dots}} only if use_gpu=True and GPU packages are required. Do not install drivers.
- Action: 
  For use_conda=True: Return 
    mkdir -p /volume/workdir\n
    cd /volume/workdir\n
    /volume/micromamba/micromamba create -y -n env python={{python_version}} pip -c conda-forge --no-channel-priority\n
    /volume/micromamba/micromamba install -y -n env <conda_packages> -c conda-forge -c defaults --no-channel-priority\n
  If pip packages are needed, append:
    /volume/micromamba/micromamba run -n env pip install --no-cache-dir <pip_packages>\n
  If use_gpu=True, append --extra-index-url https://download.pytorch.org/whl/cu{{cuda_version_no_dots}} to pip install.
  If installation fails, set bash_commands="" and provide error message with suggested fixes for Cycle 3.
  For use_conda=False: Return 
    mkdir -p /volume/workdir\n
    cd /volume/workdir\n
    python{{python_version}} -m venv venv\n
    source venv/bin/activate && pip install --upgrade pip && pip install --no-cache-dir <packages>\n
  If use_gpu=True, append --extra-index-url https://download.pytorch.org/whl/cu{{cuda_version_no_dots}} to pip install.
  If installation fails, set bash_commands="" and provide error message with suggested fixes for Cycle 3. Replace {{python_version}} with the user-specified python_version (e.g., 3.12), {{python_version_no_dots}} with Python version without dots (e.g., 312), and {{cuda_version_no_dots}} with CUDA version without dots (e.g., 124 for 12.4). Include <p>...</p> in reasoning_content explaining observation, installation results, and action. If successful, note that bash_commands are returned without 'export DEBIAN_FRONTEND=noninteractive' for user compatibility.
Example for conda: {"response_type": "json", "bash_commands": "mkdir -p /volume/workdir\ncd /volume/workdir\n/volume/micromamba/micromamba create -y -n env python=3.12 pip -c conda-forge --no-channel-priority\n/volume/micromamba/micromamba install -y -n env flask==3.0.3 fastapi==0.115.0 sqlalchemy==2.0.35 uvicorn==0.32.0 gunicorn==23.0.0 requests==2.32.3 jinja2==3.1.4 pydantic==2.9.2 httpx==0.27.2 -c conda-forge -c defaults --no-channel-priority\n/volume/micromamba/micromamba run -n env pip install --no-cache-dir psycopg2-binary==2.9.10\n", "test_script": "import flask;print('flask:',flask.__version__);import fastapi;print('fastapi:',fastapi.__version__);import sqlalchemy;print('sqlalchemy:',sqlalchemy.__version__);import uvicorn;print('uvicorn:',uvicorn.__version__);import gunicorn;print('gunicorn:',gunicorn.__version__);import requests;print('requests:',requests.__version__);import jinja2;print('jinja2:',jinja2.__version__);import pydantic;print('pydantic:',pydantic.__version__);import httpx;print('httpx:',httpx.__version__);import psycopg2;print('psycopg2:',psycopg2.__version__);import subprocess;print(subprocess.run(['pip', 'check'], capture_output=True, text=True).stdout)", "reasoning_content": "<p>Observation: Cycle 1 JSON provides commands for web development. Thought: Installation successful with compatible versions. Action: Returned installation commands and test script, excluding 'export DEBIAN_FRONTEND=noninteractive'.</p>", "message": "Environment setup commands ready for execution"}
Example for venv: {"response_type": "json", "bash_commands": "mkdir -p /volume/workdir\ncd /volume/workdir\n/python3.12 -m venv venv\nsource venv/bin/activate && pip install --upgrade pip && pip install --no-cache-dir flask==3.0.3 fastapi==0.115.0 sqlalchemy==2.0.35 uvicorn==0.32.0 gunicorn==23.0.0 requests==2.32.3 jinja2==3.1.4 pydantic==2.9.2 httpx==0.27.2 psycopg2-binary==2.9.10", "test_script": "import flask;print('flask:',flask.__version__);import fastapi;print('fastapi:',fastapi.__version__);import sqlalchemy;print('sqlalchemy:',sqlalchemy.__version__);import uvicorn;print('uvicorn:',uvicorn.__version__);import gunicorn;print('gunicorn:',gunicorn.__version__);import requests;print('requests:',requests.__version__);import jinja2;print('jinja2:',jinja2.__version__);import pydantic;print('pydantic:',pydantic.__version__);import httpx;print('httpx:',httpx.__version__);import psycopg2;print('psycopg2:',psycopg2.__version__);import subprocess;print(subprocess.run(['pip', 'check'], capture_output=True, text=True).stdout)", "reasoning_content": "<p>Observation: Cycle 1 JSON provides pip installation commands for web development. Thought: Installation successful with compatible versions. Action: Returned installation commands and test script, excluding 'export DEBIAN_FRONTEND=noninteractive'.</p>", "message": "Environment setup and tests successful"}
"""

SYSTEM_PROMPT_CYCLE3 = r"""
/think
Think step-by-step, using your internal knowledge to select compatible package versions. Reason through potential conflicts and explain your choices.
Return only a JSON object: {"response_type": "json", "bash_commands": "...", "test_script": "...", "reasoning_content": "<p>...</p>", "message": "..."}.
The response must be valid JSON without any surrounding text.
**Cycle 3: Fix Compatibility Issues**
- Observation: Receive execution logs and test results from Cycle 2, including Cycle 2 JSON response (bash_commands, test_script, reasoning_content, message), and the original User input JSON specifying the query and environment parameters (e.g., query, use_conda, use_gpu, version_preference, python_version).
- Thought: 
  1. If "use_conda": True, use Micromamba at /volume/micromamba/micromamba. Analyze logs for errors (e.g., version conflicts, unavailable packages, test failures, or missing pip). Always include 'pip' in the package list to ensure it is available for subsequent pip installations and test scripts. Reason through dependencies to resolve compatible versions across conda-forge and defaults channels. Create an environment with `micromamba create -y -n env python={{python_version}} pip`. For version conflicts, select compatible versions by adjusting package versions or using pip fallback if necessary. Install packages via Micromamba with -c conda-forge -c defaults --no-channel-priority. For unsupported packages, use /volume/micromamba/micromamba run -n env pip install --no-cache-dir <pip_packages>. If use_gpu=True, append --extra-index-url https://download.pytorch.org/whl/cu{{cuda_version_no_dots}} to pip install. If flexible channel priority fails, try reordering channels or excluding problematic packages as a last resort. If "version_preference": "stable", prefer stable packages; if "latest", prefer latest compatible packages. Generate test_script to verify all packages and include GPU checks if relevant.
  2. If "use_conda": False, use pip in a virtual environment. Analyze logs for errors (e.g., version conflicts, unavailable packages, test failures, or pip issues). Select stable and compatible package versions by reasoning through dependencies. Ensure pip is upgraded after venv creation. Include --extra-index-url https://download.pytorch.org/whl/cu{{cuda_version_no_dots}} only if use_gpu=True and GPU packages are required. Do not install drivers. Generate test_script to verify all packages and include GPU checks if relevant.
3. Analyze test logs (including pip check output) for hidden compatibility issues not caught in Cycle 2 (e.g., version conflicts or dependency mismatches). If issues are found, set {"status": "error"} and propose updated bash_commands and test_script. If no issues, retain the original bash_commands and test_script from Cycle 2, implying success for transition to Cycle 4.
- Action: 
  If errors detected (e.g., version conflicts, test failures), return {"response_type": "json", "status": "error", "bash_commands": "...", "test_script": "...", "reasoning_content": "<p>...</p>", "message": "..."} with updated commands and a rationale for changes. 
  If no errors, return {"response_type": "json", "bash_commands": "...", "test_script": "...", "reasoning_content": "<p>...</p>", "message": "..."} using Cycle 2 commands, excluding 'export DEBIAN_FRONTEND=noninteractive', and note in reasoning_content that compatibility is confirmed.
  Replace {{python_version}} with the user-specified python_version (e.g., 3.12), {{python_version_no_dots}} with Python version without dots (e.g., 312), and {{cuda_version_no_dots}} with CUDA version without dots (e.g., 124 for 12.4).
  For use_conda=True: use 
    mkdir -p /volume/workdir\n
    cd /volume/workdir\n
    /volume/micromamba/micromamba create -y -n env python={{python_version}} pip -c conda-forge --no-channel-priority\n
    /volume/micromamba/micromamba install -y -n env <conda_packages> -c conda-forge -c defaults --no-channel-priority\n
  If pip packages are needed, append:
    /volume/micromamba/micromamba run -n env pip install --no-cache-dir <pip_packages>\n
  If use_gpu=True, append --extra-index-url https://download.pytorch.org/whl/cu{{cuda_version_no_dots}} to pip install.
  For use_conda=False: use 
    mkdir -p /volume/workdir\n
    cd /volume/workdir\n
    python{{python_version}} -m venv venv\n
    source venv/bin/activate && pip install --upgrade pip && pip install --no-cache-dir <pip_packages>\n
  If use_gpu=True, append --extra-index-url https://download.pytorch.org/whl/cu{{cuda_version_no_dots}} to pip install.
  If no errors, return bash_commands and test_script from Cycle 2, ensuring 'export DEBIAN_FRONTEND=noninteractive' is excluded. Replace {{python_version}} with the user-specified python_version (e.g., 3.12), {{python_version_no_dots}} with Python version without dots (e.g., 312), and {{cuda_version_no_dots}} with CUDA version without dots (e.g., 124 for 12.4). Set test_script with import <package>; print(<package>.__version__); for each package, plus pip check, using semicolons. Include <p>...</p> in reasoning_content explaining observation, error analysis, package adjustments, and test script rationale. Set message="" unless reporting an unavoidable exclusion or pip fallback.
Example for conda: {"response_type": "json", "bash_commands": "mkdir -p /volume/workdir\ncd /volume/workdir\n/volume/micromamba/micromamba create -y -n env python=3.12 pip -c conda-forge --no-channel-priority\n/volume/micromamba/micromamba install -y -n env flask==3.0.3 fastapi==0.115.0 sqlalchemy==2.0.35 uvicorn==0.32.0 gunicorn==23.0.0 requests==2.32.3 jinja2==3.1.4 pydantic==2.9.2 httpx==0.27.2 -c conda-forge -c defaults --no-channel-priority\n/volume/micromamba/micromamba run -n env pip install --no-cache-dir psycopg2-binary==2.9.10", "test_script": "import flask;print('flask:',flask.__version__);import fastapi;print('fastapi:',fastapi.__version__);import sqlalchemy;print('sqlalchemy:',sqlalchemy.__version__);import uvicorn;print('uvicorn:',uvicorn.__version__);import gunicorn;print('gunicorn:',gunicorn.__version__);import requests;print('requests:',requests.__version__);import jinja2;print('jinja2:',jinja2.__version__);import pydantic;print('pydantic:',pydantic.__version__);import httpx;print('httpx:',httpx.__version__);import psycopg2;print('psycopg2:',psycopg2.__version__);import subprocess;print(subprocess.run(['pip', 'check'], capture_output=True, text=True).stdout)", "reasoning_content": "<p>Observation: Cycle 2 logs show psycopg2-binary not in conda-forge. Thought: Resolved by using pip for psycopg2-binary with version 2.9.10, compatible with other packages. Ensured pip is included in conda env. Action: Generated commands and test script, excluding 'export DEBIAN_FRONTEND=noninteractive'.</p>", "message": "Using pip for psycopg2-binary in conda env"}
Example for venv: {"response_type": "json", "bash_commands": "mkdir -p /volume/workdir\ncd /volume/workdir\\npython3.12 -m venv venv\nsource venv/bin/activate && pip install --upgrade pip && pip install --no-cache-dir flask==3.0.3 fastapi==0.115.0 sqlalchemy==2.0.35 uvicorn==0.32.0 gunicorn==23.0.0 requests==2.32.3 jinja2==3.1.4 pwdantic==2.9.2 httpx==0.27.2 psycopg2-binary==2.9.10", "test_script": "import flask;print('flask:',flask.__version__);import fastapi;print('fastapi:',fastapi.__version__);import sqlalchemy;print('sqlalchemy:',sqlalchemy.__version__);import uvicorn;print('uvicorn:',uvicorn.__version__);import gunicorn;print('gunicorn:',gunicorn.__version__);import requests;print('requests:',requests.__version__);import jinja2;print('jinja2:',jinja2.__version__);import pydantic;print('pydantic:',pydantic.__version__);import httpx;print('httpx:',httpx.__version__);import psycopg2;print('psycopg2:',psycopg2.__version__);import subprocess;print(subprocess.run(['pip', 'check'], capture_output=True, text=True).stdout)", "reasoning_content": "<p>Observation: Cycle 2 logs show version conflict for flask. Thought: Resolved by selecting compatible versions, ensuring pip is upgraded. Action: Generated commands and test script, excluding 'export DEBIAN_FRONTEND=noninteractive'.</p>", "message": ""}
"""

SYSTEM_PROMPT_CYCLE4 = r"""
/think
Return only a JSON object: {"response_type": "json", "bash_commands": "...", "comments": "<p>...</p>", "message": "..."}.
The response must be valid JSON without any surrounding text.
**Cycle 4: Generate Clean User-Facing Installation Commands**
- Observation: Receive the final JSON response from Cycle 2 or Cycle 3 (bash_commands, test_script, reasoning_content, message), the original User input JSON (query, use_conda, use_gpu, version_preference, python_version), and test results from Cycle 2/3 containing 'status' ('success' or 'error'), 'output', and 'error'.
- Thought: 
  - Thought: 
  1. Preserve the bash_commands from Cycle 2 or Cycle 3 as-is for internal use (e.g., may include Modal-specific paths or additional setup steps).
  2. Generate clean, user-friendly bash commands for the 'message' field, formatted for direct use on the user's machine. These commands must:
     - Remove all internal paths (e.g., /volume, /volume/micromamba) and Modal-specific details.
     - Assume the user has conda installed if use_conda=True, or Python installed if use_conda=False.
     - If use_conda=True, use `conda` commands (e.g., `conda create`, `conda install`, `conda activate`). Do NOT include commands to install Micromamba or other package managers. Use the same channels (e.g., conda-forge, defaults) and package versions as in Cycle 2/3.
     - If use_conda=False, use `python -m venv` for a virtual environment, followed by `pip install` and activation steps (e.g., `source env/bin/activate`).
     - If use_gpu=True, include GPU-specific packages (e.g., `pytorch` with `--extra-index-url https://download.pytorch.org/whl/cu121`) only if allowed by Cycle 2/3. Note in comments that CUDA drivers must be installed manually.
     - If any packages were installed via pip instead of conda, explain in comments why (e.g., not available in conda channels).
     - Include environment activation steps (e.g., `conda activate env` or `source env/bin/activate`).
     - Format commands with comments (e.g., `# Create and activate environment`) as shown in the examples below, and place them in the 'message' field.
     - If test_result['status'] == 'success', confirm the environment is functional. If test_result['status'] == 'error', use the bash_commands from Cycle 2/3 as a fallback for 'bash_commands', but generate 'message' with proposed commands, warning in comments about test failures.
    3. Include comments as <p>...</p> explaining:
     - The purpose of the commands and the environment setup.
     - Any package modifications (e.g., downgrades, exclusions, pip installations) with reasons (e.g., version conflicts, channel unavailability).
     - Additional user instructions (e.g., manual installation of NVIDIA drivers for GPU support).
     - Differences between user-requested packages and the final set, if any.
     - If test_result['status'] == 'error', include: "We could not fully test the environment due to test failures, which may be caused by incorrect test scripts or unresolved dependencies. However, we propose trying the following configuration in 'message'."
  4. Set a status message (not to be confused with the 'message' field) to confirm success or warn about limitations, included in 'comments':
     - If test_result['status'] == 'success', include in comments: "Environment setup and tests successful."
     - If test_result['status'] == 'error', include in comments: "Environment could not be fully tested due to test failures. Please try the proposed configuration in 'message' and report any issues."
- Action: 
  1. Use bash_commands from Cycle 2 or Cycle 3 for the 'bash_commands' field without modification.
  2. Generate user-friendly commands for the 'message' field, formatted as in the examples below, replacing {{python_version}} with the user-specified python_version (e.g., 3.12), {{python_version_no_dots}} with Python version without dots (e.g., 312), and {{cuda_version_no_dots}} with CUDA version without dots (e.g., 121 for 12.1).
  3. Ensure 'message' contains commands in the exact format of the examples, including comments (e.g., `# Create and activate environment`).
  For example:
  
    For use_conda=True:
    
    # Create and activate environment
    conda create -y -n webdev_env python=3.12 pip -c conda-forge --no-channel-priority
    conda activate webdev_env
    
    # Install packages
    conda install -y requests=2.32.3 flask=3.0.3 fastapi=0.115.0 httpx=0.27.2 pydantic=2.9.2 click=8.1.7 rich=13.8.1 typer=0.12.5 -c conda-forge -c defaults --no-channel-priority
    
    Environment setup complete!
    
    # If installation fails, try adjusting channel settings
    conda config --remove-key channels
    conda config --add channels conda-forge
    conda config --add channels defaults
    conda clean --all -y
    conda install -y requests=2.32.3 flask=3.0.3 fastapi=0.115.0 httpx=0.27.2 pydantic=2.9.2 click=8.1.7 rich=13.8.1 typer=0.12.5 --override-channels -c conda-forge
    
    For use_conda=False:
    
      # Create and activate virtual environment
      python -m venv webdev_env
      source webdev_env/bin/activate
      
      # Install packages
      pip install requests==2.32.3 flask=3.0.3 fastapi=0.115.0 httpx=0.27.2 pydantic=2.9.2 click=8.1.7 rich=13.8.1 typer=0.12.5
      
      Environment setup complete!
"""

def extract_json(raw_response: str) -> dict:
    raw_response = raw_response.strip()
    json_block = re.search(r'```json\n([\s\S]*?)\n```', raw_response)
    if json_block:
        try:
            return json.loads(json_block.group(1))
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON block: {str(e)}")
    try:
        return json.loads(raw_response)
    except json.JSONDecodeError:
        json_match = re.search(r'\{[\s\S]*\}', raw_response)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse JSON-like string: {str(e)}")
    raise json.JSONDecodeError("No valid JSON found in response", raw_response, 0)

logger = logging.getLogger(__name__)

def install_micromamba_if_needed():
    micromamba_path = "/volume/micromamba/micromamba"
    tarball_path = "/volume/micromamba/micromamba.tar.bz2"
    micromamba_dir = "/volume/micromamba"

    os.environ["MAMBA_ROOT_PREFIX"] = micromamba_dir
    os.environ["MAMBA_CACHE_DIR"] = f"{micromamba_dir}/cache"
    os.environ["MAMBA_NO_CACHE"] = "1"

    if os.path.exists(micromamba_path):
        logger.info("Micromamba already installed at /volume/micromamba/micromamba")
        return

    logger.info("Installing Micromamba to /volume/micromamba")
    micromamba_url = "https://micromamba.snakepit.net/api/micromamba/linux-64/latest"
    fallback_url = "https://github.com/mamba-org/micromamba-releases/releases/download/2.0.1-0/micromamba-linux-64.tar.bz2"
    max_retries = 3
    retry_delay = 5

    for attempt in range(1, max_retries + 1):
        try:
            if os.path.exists(micromamba_dir):
                subprocess.run(["rm", "-rf", micromamba_dir], check=True)
                logger.info(f"Removed existing {micromamba_dir}")
            os.makedirs(micromamba_dir, exist_ok=True)

            url = micromamba_url if attempt <= 2 else fallback_url
            logger.info(f"Attempt {attempt}/{max_retries}: Downloading Micromamba from {url}")
            with requests.get(url, stream=True, timeout=2000):
                r.raise_for_status()
                with open(tarball_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

            if not os.path.exists(tarball_path) or os.path.getsize(tarball_path) == 0:
                raise RuntimeError(f"Failed to download Micromamba: {tarball_path} is missing or empty")

            with open(setup_log_path, "a") as setup_log:
                setup_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Listing tarball contents\n")
            result = subprocess.run(
                ["tar", "-tjf", tarball_path],
                capture_output=True,
                text=True,
                check=True
            )
            logger.info(f"Tarball contents:\n{result.stdout}")
            with open(setup_log_path, "a") as setup_log:
                setup_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Tarball contents:\n{result.stdout}\n")

            with open(setup_log_path, "a") as setup_log:
                setup_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Extracting Micromamba\n")
            result = subprocess.run(
                ["tar", "-xjf", tarball_path, "-C", micromamba_dir],
                capture_output=True,
                text=True,
                check=True
            )
            logger.info(f"Extraction output: {result.stdout}")

            os.remove(tarball_path)

            find_result = subprocess.run(
                ["find", micromamba_dir, "-type", "f", "-name", "micromamba"],
                capture_output=True,
                text=True,
                check=True
            )
            found_paths = find_result.stdout.strip().split("\n")
            if not found_paths or not found_paths[0]:
                raise RuntimeError("Micromamba binary not found after extraction")

            binary_path = found_paths[0]
            os.rename(binary_path, micromamba_path)
            logger.info(f"Moved Micromamba binary from {binary_path} to {micromamba_path}")

            os.chmod(micromamba_path, 0o755)
            logger.info("Micromamba installed successfully")
            break

        except (requests.RequestException, subprocess.CalledProcessError, OSError) as e:
            logger.error(f"Attempt {attempt} failed: {str(e)}")
            with open(setup_log_path, "a") as setup_log:
                setup_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Micromamba installation attempt {attempt} failed: {str(e)}\n")
            if attempt == max_retries:
                raise RuntimeError(f"Micromamba installation failed after {max_retries} attempts: {str(e)}")
            time.sleep(retry_delay)

setup_log_path = "/volume/setup_log.txt"
test_log_path = "/volume/test_log.txt"
result_path = "/volume/result.json"
lock_file = "/volume/micromamba/lockfile"


@app.function(
    image=image_311,
    gpu="A10",
    volumes={"/volume": modal.Volume.from_name("claude-test-cache")},
    timeout=3600,
    secrets=[modal.Secret.from_name("anthropic-secret")],
    max_containers=1
)
def run_test_311(commands: list, test_script: str, use_conda: bool = False):
    run_id = uuid.uuid4().hex[:8]
    logger.info(f"Running test [Run ID: {run_id}] with commands: {commands}, use_conda: {use_conda}")
    cache_dir = None

    try:
        if use_conda:
            os.makedirs(os.path.dirname(lock_file), exist_ok=True)
            with open(lock_file, "w") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                logger.info(f"Acquired lock [Run ID: {run_id}]")

        if use_conda:
            cache_dir = f"/volume/micromamba/cache_{run_id}"
            os.makedirs(cache_dir, exist_ok=True)
            os.environ["MAMBA_ROOT_PREFIX"] = "/volume/micromamba"
            os.environ["MAMBA_CACHE_DIR"] = cache_dir
            logger.info(f"MAMBA_ROOT_PREFIX=/volume/micromamba, MAMBA_CACHE_DIR={cache_dir}")
            micromamba_check = subprocess.run(
                ["/volume/micromamba/micromamba", "--version"],
                capture_output=True,
                text=True
            )
            logger.info(f"Micromamba version: {micromamba_check.stdout.strip()}")

        for log_file in [setup_log_path, test_log_path, result_path]:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            if not os.path.exists(log_file):
                with open(log_file, "w") as f:
                    f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Log file created\n")
                logger.info(f"Created log file: {log_file}")

        with open("/volume/setup.sh", "w") as f:
            f.write("#!/bin/bash\nset -e\n")
            if use_conda:
                f.write("export MAMBA_ROOT_PREFIX=/volume/micromamba\n")
                f.write(f"export MAMBA_CACHE_DIR={cache_dir}\n")
            f.write("\n".join(commands) + "\n")
        os.chmod("/volume/setup.sh", 0o755)

        logger.info("Executing setup script /volume/setup.sh")
        start_time = time.time()
        with open(setup_log_path, "a") as setup_log:
            setup_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Executing setup:\n")
            result = subprocess.run(
                ["/volume/setup.sh"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3600
            )
            setup_log.write(f"STDOUT: {result.stdout}\n")
            setup_log.write(f"STDERR: {result.stderr}\n")
            setup_log.flush()
            return_code = result.returncode
            logger.info(f"Setup script completed with return code: {return_code}")
            logger.info(f"Setup took {time.time() - start_time:.2f} seconds")
            setup_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Setup completed with return code: {return_code}\n")
            if return_code != 0:
                logger.error(f"Setup failed: {result.stderr}")
                return {"status": "error", "output": result.stdout[-100:], "error": result.stderr[-100:]}

        python_path = "/volume/workdir/venv/bin/python" if not use_conda else "/volume/micromamba/envs/env/bin/python"
        if not os.path.exists(python_path):
            logger.error(f"Environment not found at {python_path}")
            return {"status": "error", "output": "", "error": f"Environment not found at {python_path}"}

        if test_script:
            logger.info("Writing test script to /volume/test.py")
            with open("/volume/test.py", "w") as f:
                test_lines = [line.strip() for line in test_script.split(";") if line.strip()]
                f.write("\n".join(test_lines))
            logger.info(f"Executing test script: /volume/test.py")
            with open(test_log_path, "a") as test_log:
                test_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting test:\n")
                cmd = (
                    ["/volume/micromamba/micromamba", "run", "-n", "env", "python", "/volume/test.py"]
                    if use_conda else
                    [python_path, "/volume/test.py"]
                )
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=3600
                )
                test_log.write(f"STDOUT: {result.stdout}\n")
                test_log.write(f"STDERR: {result.stderr}\n")
                test_log.flush()
                return_code = result.returncode
                logger.info(f"Test script completed with return code: {return_code}")
                test_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Test completed with return code: {return_code}\n")
                if return_code != 0:
                    logger.error(f"Test failed: {result.stderr}")
                    return {"status": "error", "output": result.stdout[-100:], "error": result.stderr[-100:]}

        return {"status": "success", "output": "", "error": ""}

    except Exception as e:
        logger.error(f"Test failed [Run ID: {run_id}]: {str(e)}")
        try:
            with open(setup_log_path, "a") as setup_log:
                setup_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Exception: {str(e)}\n")
        except Exception as log_error:
            logger.error(f"Failed to write to setup log: {str(log_error)}")
        return {"status": "error", "output": "", "error": str(e)}
    finally:
        try:
            if use_conda:
                subprocess.run(
                    ["/volume/micromamba/micromamba", "env", "remove", "-n", "env", "--yes", "--root-prefix", "/volume/micromamba"],
                    capture_output=True,
                    text=True
                )
                logger.info(f"Removed Conda environment [Run ID: {run_id}]")
            else:
                subprocess.run(
                    ["rm", "-rf", "/volume/workdir"],
                    capture_output=True,
                    text=True
                )
                logger.info(f"Removed venv environment [Run ID: {run_id}]")
            if use_conda and os.path.exists(lock_file):
                with open(lock_file, "w") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                logger.info(f"Released lock [Run ID: {run_id}]")
        except Exception as cleanup_error:
            logger.error(f"Cleanup failed [Run ID: {run_id}]: {str(cleanup_error)}")

@app.function(
    image=image_312,
    gpu="A10",
    volumes={"/volume": modal.Volume.from_name("claude-test-cache")},
    timeout=3600,
    secrets=[modal.Secret.from_name("anthropic-secret")],
    max_containers=1
)
def run_test_312(commands: list, test_script: str, use_conda: bool = False):
    run_id = uuid.uuid4().hex[:8]
    logger.info(f"Running test [Run ID: {run_id}] with commands: {commands}, use_conda: {use_conda}")
    cache_dir = None

    try:
        if use_conda:
            os.makedirs(os.path.dirname(lock_file), exist_ok=True)
            with open(lock_file, "w") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                logger.info(f"Acquired lock [Run ID: {run_id}]")

        if use_conda:
            cache_dir = f"/volume/micromamba/cache_{run_id}"
            os.makedirs(cache_dir, exist_ok=True)
            os.environ["MAMBA_ROOT_PREFIX"] = "/volume/micromamba"
            os.environ["MAMBA_CACHE_DIR"] = cache_dir
            logger.info(f"MAMBA_ROOT_PREFIX=/volume/micromamba, MAMBA_CACHE_DIR={cache_dir}")
            micromamba_check = subprocess.run(
                ["/volume/micromamba/micromamba", "--version"],
                capture_output=True,
                text=True
            )
            logger.info(f"Micromamba version: {micromamba_check.stdout.strip()}")

        for log_file in [setup_log_path, test_log_path, result_path]:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            if not os.path.exists(log_file):
                with open(log_file, "w") as f:
                    f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Log file created\n")
                logger.info(f"Created log file: {log_file}")

        with open("/volume/setup.sh", "w") as f:
            f.write("#!/bin/bash\nset -e\n")
            if use_conda:
                f.write("export MAMBA_ROOT_PREFIX=/volume/micromamba\n")
                f.write(f"export MAMBA_CACHE_DIR={cache_dir}\n")
            f.write("\n".join(commands) + "\n")
        os.chmod("/volume/setup.sh", 0o755)

        logger.info("Executing setup script /volume/setup.sh")
        start_time = time.time()
        with open(setup_log_path, "a") as setup_log:
            setup_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Executing setup:\n")
            result = subprocess.run(
                ["/volume/setup.sh"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3600
            )
            setup_log.write(f"STDOUT: {result.stdout}\n")
            setup_log.write(f"STDERR: {result.stderr}\n")
            setup_log.flush()
            return_code = result.returncode
            logger.info(f"Setup script completed with return code: {return_code}")
            logger.info(f"Setup took {time.time() - start_time:.2f} seconds")
            setup_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Setup completed with return code: {return_code}\n")
            if return_code != 0:
                logger.error(f"Setup failed: {result.stderr}")
                return {"status": "error", "output": result.stdout[-100:], "error": result.stderr[-100:]}

        python_path = "/volume/workdir/venv/bin/python" if not use_conda else "/volume/micromamba/envs/env/bin/python"
        if not os.path.exists(python_path):
            logger.error(f"Environment not found at {python_path}")
            return {"status": "error", "output": "", "error": f"Environment not found at {python_path}"}

        if test_script:
            logger.info("Writing test script to /volume/test.py")
            with open("/volume/test.py", "w") as f:
                test_lines = [line.strip() for line in test_script.split(";") if line.strip()]
                f.write("\n".join(test_lines))
            logger.info(f"Executing test script: /volume/test.py")
            with open(test_log_path, "a") as test_log:
                test_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting test:\n")
                cmd = (
                    ["/volume/micromamba/micromamba", "run", "-n", "env", "python", "/volume/test.py"]
                    if use_conda else
                    [python_path, "/volume/test.py"]
                )
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=3600
                )
                test_log.write(f"STDOUT: {result.stdout}\n")
                test_log.write(f"STDERR: {result.stderr}\n")
                test_log.flush()
                return_code = result.returncode
                logger.info(f"Test script completed with return code: {return_code}")
                test_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Test completed with return code: {return_code}\n")
                if return_code != 0:
                    logger.error(f"Test failed: {result.stderr}")
                    return {"status": "error", "output": result.stdout[-100:], "error": result.stderr[-100:]}

        return {"status": "success", "output": "", "error": ""}

    except Exception as e:
        logger.error(f"Test failed [Run ID: {run_id}]: {str(e)}")
        try:
            with open(setup_log_path, "a") as setup_log:
                setup_log.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Exception: {str(e)}\n")
        except Exception as log_error:
            logger.error(f"Failed to write to setup log: {str(log_error)}")
        return {"status": "error", "output": "", "error": str(e)}
    finally:
        try:
            if use_conda:
                subprocess.run(
                    ["/volume/micromamba/micromamba", "env", "remove", "-n", "env", "--yes", "--root-prefix", "/volume/micromamba"],
                    capture_output=True,
                    text=True
                )
                logger.info(f"Removed Conda environment [Run ID: {run_id}]")
            else:
                subprocess.run(
                    ["rm", "-rf", "/volume/workdir"],
                    capture_output=True,
                    text=True
                )
                logger.info(f"Removed venv environment [Run ID: {run_id}]")
            if use_conda and os.path.exists(lock_file):
                with open(lock_file, "w") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                logger.info(f"Released lock [Run ID: {run_id}]")
        except Exception as cleanup_error:
            logger.error(f"Cleanup failed [Run ID: {run_id}]: {str(cleanup_error)}")


async def async_generator_wrapper(generator):
    """Convert a synchronous generator to an async iterable."""
    for item in generator:
        yield item
        await asyncio.sleep(0)

@app.function(
    image=image_312,
    timeout=3600,
    volumes={"/volume": modal.Volume.from_name("claude-test-cache")},
    secrets=[modal.Secret.from_name("anthropic-secret")]
)
async def process_user_input_cycle4(user_input: dict, cycle_response: dict, test_result: dict, python_version: str) -> dict:
    logger.info("Cycle 4: Sending bash_commands to Claude for cleaning")
    bash_commands = cycle_response.get("bash_commands", "")
    if not bash_commands:
        logger.error("No bash_commands provided in cycle_response")
        return {
            "response_type": "json",
            "bash_commands": "",
            "message": "Error: Unable to generate user-facing commands due to missing input."
        }

    query_data = {
        "bash_commands": bash_commands,
        "test_result": test_result,
        "user_input": user_input,
        "python_version": python_version
    }

    try:
        client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{"role": "user", "content": SYSTEM_PROMPT_CYCLE4 + "\n\nInput data: " + json.dumps(query_data)}]
        )
        raw_response = response.content[0].text.strip()
        logger.info(f"Claude raw response: {raw_response}")
        if not raw_response:
            raise ValueError("Empty response from Claude")
        claude_response = json.loads(raw_response)
        required_keys = {"response_type", "bash_commands", "message"}
        if not all(claude_response.get(key) for key in required_keys):
            logger.error(f"Invalid Claude response: missing required keys {required_keys - set(claude_response.keys())}")
            return {
                "response_type": "json",
                "bash_commands": bash_commands,
                "message": "Error: Unable to generate user-facing commands."
            }
        logger.info(f"Cycle 4 response: {claude_response}")
        return claude_response
    except APIError as e:
        logger.error(f"Claude API error: {str(e)}")
        return {
            "response_type": "json",
            "bash_commands": bash_commands,
            "message": f"Error: Claude API failed: {str(e)}."
        }
    except Exception as e:
        logger.error(f"Unexpected error in Cycle 4: {str(e)}")
        return {
            "response_type": "json",
            "bash_commands": bash_commands,
            "message": f"Error: {str(e)}"
        }

@app.function(
    image=image_312,
    timeout=3600,
    secrets=[modal.Secret.from_name("anthropic-secret")],
    volumes={"/volume": modal.Volume.from_name("claude-test-cache")},
    max_containers=1
)
async def process_user_input_logic(user_input: dict, python_version: str):
    app_log_path = "/volume/app_log.txt"
    result_path = "/volume/result.json"
    run_id = uuid.uuid4().hex[:8]

    for log_file in [setup_log_path, app_log_path, result_path]:
        try:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            if not os.path.exists(log_file):
                with open(log_file, "w") as f:
                    f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Log file created\n")
                logger.info(f"Created log file: {log_file}")
        except Exception as e:
            logger.error(f"Error creating log file {log_file}: {str(e)}")
            yield json.dumps({
                "status": "done",
                "result": json.dumps({
                    "message": "We could not test the environment. Please try again."
                })
            }) + "\n"
            return

    file_handler = None
    try:
        file_handler = logging.FileHandler(app_log_path)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(file_handler)
    except Exception as e:
        logger.error(f"Error setting up file handler for {app_log_path}: {str(e)}")
        yield json.dumps({
            "status": "done",
            "result": json.dumps({
                "message": "We could not test the environment. Please try again."
            })
        }) + "\n"
        return

    try:
        client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        use_conda = user_input.get("use_conda", False)
        if use_conda:
            install_micromamba_if_needed()
            os.environ["PATH"] = f"/volume/micromamba:{os.environ['PATH']}"

        query = user_input.get("query", "").lower().strip()
        if not query:
            yield json.dumps({
                "status": "done",
                "result": json.dumps({
                    "message": "We could not test the environment. Please try again."
                })
            }) + "\n"
            return

        try:
            full_query = {
                "query": query,
                "use_conda": use_conda,
                "use_gpu": use_gpu,
                "version_preference": version_preference,
                "python_version": python_version
            }
        except NameError as e:
            raise ValueError(f"Undefined variable: {e}")

        max_attempts = 3
        attempt = 1
        cycle1_response = None
        cycle2_response = None
        test_result = None

        def clean_bash_commands(bash_commands: str) -> str:
            lines = bash_commands.splitlines()
            cleaned_lines = [line for line in lines if "export DEBIAN_FRONTEND=noninteractive" not in line]
            return "\n".join(cleaned_lines)

        start_time = time.time()
        last_keepalive = start_time

        while attempt <= max_attempts:
            current_time = time.time()
            if current_time - last_keepalive >= 60:
                yield json.dumps({
                    "status": "working",
                    "message": "Relax, have some tea, we're doing our thing"
                }) + "\n"
                last_keepalive = current_time
                await asyncio.sleep(0.1)

            logger.info(f"Attempt {attempt} of {max_attempts} [Run ID: {run_id}]")

            if not cycle1_response:
                logger.info("Cycle 1: Generating bash commands and test script")
                cycle_start = time.time()
                try:
                    response = client.messages.create(
                        model="claude-sonnet-4-20250514",
                        max_tokens=4000,
                        messages=[{"role": "user", "content": SYSTEM_PROMPT_CYCLE1 + "\n\nUser input: " + json.dumps(full_query)}]
                    )
                    raw_response = response.content[0].text.strip()
                    logger.info(f"Cycle 1 raw response: {raw_response}")
                    if not raw_response:
                        raise ValueError("Empty response from Claude")
                    cycle1_response = extract_json(raw_response)
                    cycle1_response["bash_commands"] = clean_bash_commands(cycle1_response["bash_commands"])
                    logger.info(f"Cycle 1 parsed response: {json.dumps(cycle1_response)}")
                    with open(result_path, "a") as result_file:
                        result_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Cycle 1 response: {json.dumps(cycle1_response)}\n")
                    if cycle1_response.get("message") == "Please, specify your request":
                        yield json.dumps({
                            "status": "done",
                            "result": json.dumps({
                                "message": cycle1_response.get("message", "We could not test the environment. Please try again.")
                            })
                        }) + "\n"
                        return
                except (APIError, ValueError, json.JSONDecodeError) as e:
                    logger.error(f"Cycle 1 failed: {str(e)}")
                    yield json.dumps({
                        "status": "done",
                        "result": json.dumps({
                            "message": "We could not test the environment. Please try again."
                        })
                    }) + "\n"
                    return
                logger.info(f"Cycle 1 took {time.time() - cycle_start:.2f} seconds")

            if not cycle2_response:
                logger.info("Cycle 2: Generating bash commands for execution")
                cycle_start = time.time()
                try:
                    response = client.messages.create(
                        model="claude-sonnet-4-20250514",
                        max_tokens=4000,
                        messages=[{"role": "user", "content": SYSTEM_PROMPT_CYCLE2 + "\n\nCycle 1 response: " + json.dumps(cycle1_response) + "\n\nUser input: " + json.dumps(full_query)}]
                    )
                    raw_response = response.content[0].text.strip()
                    logger.info(f"Cycle 2 raw_response: {raw_response}")
                    if not raw_response:
                        raise ValueError("Invalid response from Claude")
                    cycle2_response = extract_json(raw_response)
                    cycle2_response["bash_commands"] = clean_bash_commands(cycle2_response["bash_commands"])
                    logger.info(f"Cycle 2 parsed response: {json.dumps(cycle2_response)}")
                    with open(result_path, "a") as result_file:
                        result_file.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Cycle 2 response: {json.dumps(cycle2_response)}\n")
                except Exception as e:
                    logger.error(f"Cycle 2 failed: {str(e)}")
                    yield json.dumps({
                        "status": "done",
                        "result": json.dumps({
                            "message": "We could not test the environment. Please try again."
                        })
                    }) + "\n"
                    return
                logger.info(f"Cycle 2 took {time.time() - cycle_start:.2f} seconds")

            try:
                yield json.dumps({
                    "status": "working",
                    "message": "Relax, have some tea, we're doing our thing"
                }) + "\n"
                await asyncio.sleep(0.1)
                run_test_func = run_test_311 if python_version == "3.11" else run_test_312
                logger.info(
                    f"Running test [Run ID: {run_id}] with commands: {cycle2_response['bash_commands'].splitlines()}")
                cycle_start = time.time()
                test_result = await run_test_func.remote.aio(
                    cycle2_response["bash_commands"].strip().splitlines(),
                    cycle2_response["test_script"],
                    use_conda
                )
                logger.info(f"Test completed [Run ID: {run_id}] with status: {test_result.get('status')}")

                # Всегда запускаем Cycle 3 для анализа
                with open(test_log_path, "r") as test_log:
                    test_logs = test_log.read()
                logger.info("Cycle 3: Analyzing compatibility issues")
                cycle_start = time.time()
                try:
                    response = client.messages.create(
                        model="claude-sonnet-4-20250514",
                        max_tokens=4000,
                        messages=[{"role": "user", "content": SYSTEM_PROMPT_CYCLE3 + "\n\nUser input: " + json.dumps(
                            full_query) + "\n\nCycle 2 response: " + json.dumps(
                            cycle2_response) + "\n\nTest result: " + json.dumps(
                            test_result) + "\n\nTest logs: " + test_logs}]
                    )
                    raw_response = response.content[0].text.strip()
                    logger.info(f"Cycle 3 raw response: {raw_response}")
                    if not raw_response:
                        raise ValueError("Empty response from Claude")
                    cycle3_response = extract_json(raw_response)
                    cycle3_response["bash_commands"] = clean_bash_commands(cycle3_response["bash_commands"])
                    logger.info(f"Cycle 3 parsed response: {cycle3_response}")
                    with open(result_path, "a") as result_file:
                        result_file.write(
                            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Cycle 3 response: {json.dumps(cycle3_response)}\n")
                    logger.info(f"Cycle 3 took {time.time() - cycle_start:.2f} seconds")

                    # Проверяем решение Cycle 3
                    if cycle3_response.get("status") == "error" and attempt < max_attempts:
                        logger.info("Cycle 3 detected issues, updating test result and returning to Cycle 2")
                        test_result = {"status": "error", "output": "",
                                       "error": cycle3_response.get("message", "Compatibility issues detected")}
                        cycle2_response["bash_commands"] = cycle3_response["bash_commands"]
                        cycle2_response["test_script"] = cycle3_response.get("test_script",
                                                                            cycle2_response["test_script"])
                        continue  # Возвращаемся на Cycle 2 для повторного теста
                    else:
                        logger.info("Cycle 3 confirmed compatibility, proceeding to Cycle 4")
                        # Оставляем test_result как есть (обычно "success") и переходим к Cycle 4
                except (APIError, ValueError, json.JSONDecodeError) as e:
                    logger.error(f"Cycle 3 failed: {str(e)}")
                    with open(result_path, "a") as result_file:
                        result_file.write(
                            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Cycle 3 failed: {str(e)}\n")
                    yield json.dumps({
                        "status": "done",
                        "result": json.dumps({
                            "message": "We could not test the environment. Please try again."
                        })
                    }) + "\n"
                    return

                # Переход к Cycle 4 после успешного анализа
                logger.info(f"Running Cycle 4 for user-facing output [Run ID: {run_id}]")
                cycle_start = time.time()
                try:
                    cycle4_response = await process_user_input_cycle4.remote.aio(user_input, cycle2_response,
                                                                                test_result, python_version)
                    logger.info(f"Cycle 4 took {time.time() - cycle_start:.2f} seconds")
                    yield json.dumps({
                        "status": "done",
                        "result": json.dumps({
                            "message": cycle4_response.get("message",
                                                          "We could not test the environment. Please try again.")
                        })
                    }) + "\n"
                    return
                except Exception as e:
                    logger.error(f"Cycle 4 failed: {str(e)}")
                    with open(result_path, "a") as result_file:
                        result_file.write(
                            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Cycle 4 failed: {str(e)}\n")
                    yield json.dumps({
                        "status": "done",
                        "result": json.dumps({
                            "message": "We could not test the environment. Please try again."
                        })
                    }) + "\n"
                    return
            except Exception as e:
                logger.error(f"Test execution failed [Run ID: {run_id}]: {str(e)}")
                with open(result_path, "a") as result_file:
                    result_file.write(
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Test execution failed: {str(e)}\n")
                yield json.dumps({
                    "status": "done",
                    "result": json.dumps({
                        "message": "We could not test the environment. Please try again."
                    })
                }) + "\n"
                return

            attempt += 1

        yield json.dumps({
            "status": "done",
            "result": json.dumps({
                "message": "We could not test the environment. Please try again."
            })
        }) + "\n"

    finally:
        if file_handler:
            logger.removeHandler(file_handler)
            file_handler.close()

@app.function(
    image=image_311,
    timeout=7200,
    volumes={"/volume": modal.Volume.from_name("claude-test-cache")},
    secrets=[modal.Secret.from_dict({"ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY")})],
    max_containers=1
)
async def process_user_input_311(user_input: dict):
    gen = process_user_input_logic.remote_gen(user_input, "3.11")
    async for result in async_generator_wrapper(gen):
        yield result

@app.function(
    image=image_312,
    timeout=7200,
    volumes={"/volume": modal.Volume.from_name("claude-test-cache")},
    secrets=[modal.Secret.from_dict({"ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY")})],
    max_containers=1
)
async def process_user_input_312(user_input: dict):
    gen = process_user_input_logic.remote_gen(user_input, "3.12")
    async for result in async_generator_wrapper(gen):
        yield result

@app.function(
    image=image_311,
    timeout=7200,
    min_containers=0,
    max_containers=2,
    secrets=[modal.Secret.from_name("anthropic-secret")]
)
@modal.fastapi_endpoint(method="POST", label="main-endpoint")
async def main(user_input: dict):
    start_time = time.time()
    run_id = uuid.uuid4().hex[:8]
    logger.info(f"Main function started [Run ID: {run_id}] with input: {user_input}")

    async def stream_response():
        try:
            python_version = user_input.get("python_version", "3.11")
            process_func = process_user_input_311 if python_version == "3.11" else process_user_input_312
            gen = process_func.remote_gen(user_input)
            async for data in async_generator_wrapper(gen):
                yield data
            elapsed_time = time.time() - start_time
            logger.info(f"Main function completed in {elapsed_time:.2f} seconds [Run ID: {run_id}]")
        except Exception as e:
            elapsed_time = time.time() - start_time
            logger.error(f"Main function failed after {elapsed_time:.2f} seconds [Run ID: {run_id}]: {str(e)}")
            yield json.dumps({
                "status": "done",
                "result": json.dumps({
                    "response_type": "error",
                    "bash_commands": "",
                    "message": f"Error: {str(e)}"
                })
            }) + "\n"

    return StreamingResponse(stream_response(), media_type="application/json")