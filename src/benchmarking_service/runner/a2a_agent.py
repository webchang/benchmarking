"""Async A2A agent client, wrapping the `a2a-sdk` streaming send_message flow.

Port of the upstream `a2a_client.py` without the OTEL plumbing. `a2a-sdk` is
imported lazily so the module can be imported without the SDK present.
"""

import logging
import uuid

logger = logging.getLogger(__name__)


class A2ATaskError(RuntimeError):
    """The A2A task ended in a terminal failure state (failed/rejected/canceled)."""


class A2AAgentClient:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 300.0):
        self._base_url = base_url
        self._token = token
        self._timeout = timeout

    async def send_prompt(self, prompt: str, session_id: str | None = None) -> str:
        import httpx
        from a2a.client import ClientConfig, ClientFactory, create_text_message_object
        from a2a.client.card_resolver import A2ACardResolver
        from a2a.types import Role, TaskState, TextPart

        headers = {"x-session-id": uuid.uuid4().hex}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        httpx_client = httpx.AsyncClient(timeout=self._timeout, headers=headers)
        try:
            resolver = A2ACardResolver(httpx_client=httpx_client, base_url=self._base_url)
            card = await resolver.get_agent_card()
            # The card advertises its public URL; override to the in-cluster address we dialed.
            card.url = self._base_url
            client = ClientFactory(ClientConfig(httpx_client=httpx_client)).create(card=card)

            message = create_text_message_object(role=Role.user, content=prompt)
            request_metadata = {"session_id": session_id} if session_id else None

            result_text = ""
            final_state = None
            async for response in client.send_message(message, request_metadata=request_metadata):
                if isinstance(response, tuple):
                    task, event = response
                    if task.status and task.status.state:
                        final_state = task.status.state
                    if event is not None and getattr(event, "artifact", None):
                        for part in event.artifact.parts:
                            if hasattr(part, "root") and isinstance(part.root, TextPart):
                                result_text += part.root.text
                elif hasattr(response, "parts"):
                    for part in response.parts:
                        if hasattr(part, "root") and isinstance(part.root, TextPart):
                            result_text += part.root.text

            if final_state in (TaskState.failed, TaskState.rejected, TaskState.canceled):
                detail = result_text.strip() or "<no message>"
                raise A2ATaskError(f"A2A task ended in state '{final_state.value}': {detail}")
            return result_text
        finally:
            await httpx_client.aclose()
