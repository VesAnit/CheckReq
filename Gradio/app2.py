import requests
import json
import logging
import gradio as gr
from fastapi import FastAPI
from gradio.routes import mount_gradio_app
import time
import modal

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

app = modal.App("gradio-app")
web_image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "fastapi[standard]==0.115.4",
    "gradio==5.3.0",
    "pillow~=10.2.0",
)

MODAL_URL = "https://vesanit--main-endpoint.modal.run"
MODAL_TOKEN = "<your-modal-token>"  # Replace or use os.getenv("MODAL_TOKEN")


def process_input(config_type, conda_choice, gpu_choice, python_version, state=None):
    if state is None:
        state = {}
    state["config_type"] = config_type
    state["conda_choice"] = conda_choice
    state["gpu_choice"] = gpu_choice
    state["python_version"] = python_version
    state["options_confirmed"] = True
    state["user_input"] = {
        "query": "",
        "use_conda": conda_choice == "Yes",
        "use_gpu": gpu_choice == "Yes",
        "version_preference": "latest" if config_type == "Latest Packages" else "stable",
        "python_version": python_version
    }
    logger.info(f"Processed input: {json.dumps(state['user_input'])}")
    return "Got it, please enter your request below.", state


def handle_query(query, state=None):
    if state is None:
        state = {}
    logger.debug(f"Handling query: {query}")
    if not state.get("options_confirmed", False):
        return "Please confirm the options above first.", state
    if not query.strip():
        return "Query cannot be empty. Please enter your request.", state

    state["query"] = query
    user_input = state.get("user_input", {})
    user_input["query"] = query
    state["user_input"] = user_input

    start_time = time.time()
    logger.info(f"Sending to Modal: {json.dumps(user_input)}")

    try:
        headers = {
            "Authorization": f"Bearer {MODAL_TOKEN}",
            "Content-Type": "application/json",
            "Connection": "keep-alive"
        }
        with requests.post(
                MODAL_URL,
                json=user_input,
                headers=headers,
                timeout=7200,  # 2 hours
                stream=True  # Enable streaming
        ) as response:
            response.raise_for_status()
            output = "Relax, have some tea, we're doing our thing"
            for line in response.iter_lines():
                if line:
                    data = json.loads(line.decode('utf-8'))
                    if data.get("status") == "working":
                        output = "Relax, have some tea, we're doing our thing"
                        yield output, state  # Yield to update gr.Textbox
                    elif data.get("status") == "done":
                        result_data = json.loads(data["result"])
                        message = result_data.get("message", "Error: No message provided in response.")
                        elapsed_time = time.time() - start_time
                        logger.info(f"Query completed in {elapsed_time:.2f} seconds")
                        yield message, state  # Final result
                        break

    except requests.Timeout as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Request timed out after {elapsed_time:.2f} seconds: {str(e)}")
        return f"Error: Request timed out after {elapsed_time:.2f} seconds.", state
    except requests.RequestException as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Modal call failed after {elapsed_time:.2f} seconds: {str(e)}")
        return f"Error: Failed to call Modal: {str(e)}", state
    except json.JSONDecodeError as e:
        elapsed_time = time.time() - start_time
        logger.error(f"Failed to parse Modal response after {elapsed_time:.2f} seconds: {str(e)}")
        return f"Error: Invalid response from server: {str(e)}", state


def create_gradio_interface():
    theme = gr.themes.Base(
        primary_hue="orange",
        secondary_hue="gray",
        neutral_hue="gray",
        text_size="lg",
        radius_size="sm",
    ).set(
        body_background_fill="#1e1e1e",
        body_text_color="#f0f0f0",
        button_primary_background_fill="#ff5e1a",
        button_primary_text_color="#ffffff",
        button_primary_background_fill_hover="#db551d",
        button_secondary_background_fill="#4b4b4b",
        button_secondary_background_fill_hover="#4b4b4b",
        button_secondary_text_color="#f0f0f0",
        input_background_fill="#2c2c2c",
        input_border_color="#4b4b4b",
        input_placeholder_color="#cccccc",
        block_background_fill="#2c2c2c",
        block_border_color="#4b4b4b",
        block_label_text_color="#cccccc",
        block_title_text_color="#ffffff",
    )

    with gr.Blocks(theme=theme) as demo:
        gr.Markdown("# CheckReq")
        gr.Markdown("Environment verification by resolving compatible dependencies (Linux only)")

        state = gr.State()

        config_type = gr.Radio(
            choices=["Latest Packages", "Stable Configuration"],
            label="Choose Configuration",
            value="Stable Configuration"
        )

        conda_choice = gr.Radio(
            choices=["Yes", "No"],
            label="Do you want to create a configuration for conda environment?",
            value="No"
        )

        gpu_choice = gr.Radio(
            choices=["Yes", "No"],
            label="Packages for GPU?",
            value="No"
        )

        python_version = gr.Radio(
            choices=["3.11", "3.12"],
            label="Choose Python Version",
            value="3.11"
        )

        submit = gr.Button("Confirm", variant="primary")
        output = gr.Textbox(label="Result")

        gr.Markdown("""
        Enter your environment build request, for example:
        - "I need an environment for machine learning with CUDA 12.4"
        - "I already have flask version 2.3.2 installed, need to complete the environment for web apps"

        **Note**: If you select "Packages for GPU?", specify your CUDA version in the request (e.g., "CUDA 12.4").
        To check your CUDA version, run in terminal: 
        nvidia-smi
        """)

        query_input = gr.Textbox(label="Your Environment Query", placeholder="Enter your request here...")
        query_submit = gr.Button("Run", variant="primary")
        query_output = gr.Textbox(label="Query Result")

        submit.click(
            fn=process_input,
            inputs=[config_type, conda_choice, gpu_choice, python_version, state],
            outputs=[output, state]
        )

        query_submit.click(
            fn=handle_query,
            inputs=[query_input, state],
            outputs=[query_output, state],
            queue=True,
            concurrency_limit=1  # Limit concurrency for streaming
        )

        demo.queue(max_size=10)
    return demo


@app.function(
    image=web_image,
    min_containers=1,
    scaledown_window=60 * 60,
    max_containers=2,
    timeout=7200
)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def ui():
    fastapi_app = FastAPI()
    demo = create_gradio_interface()
    return mount_gradio_app(app=fastapi_app, blocks=demo, path="/")