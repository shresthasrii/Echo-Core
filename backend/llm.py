import logging
import json
from typing import List, Dict, Any, Optional
from groq import AsyncGroq, APIError, RateLimitError
from backend.config import settings

logger = logging.getLogger("echo_core.llm")

class GroqLLM:
    """
    Groq client with automatic model fallback, retry logic, and tool call recovery.
    Primary: openai/gpt-oss-120b (fast, large context)
    Fallback: qwen/qwen3.8-27b
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        primary_model: Optional[str] = None,
        fallback_model: Optional[str] = None
    ):
        self.api_key = api_key or settings.GROQ_API_KEY
        self.primary_model = primary_model or settings.GROQ_MODEL
        self.fallback_model = fallback_model or settings.GROQ_FALLBACK_MODEL
        self.client: Optional[AsyncGroq] = None
        self._init_client()

    def _init_client(self):
        try:
            if not self.api_key:
                logger.warning("GROQ_API_KEY not configured. LLM will use deterministic fallback.")
                return
            self.client = AsyncGroq(api_key=self.api_key)
        except Exception as e:
            logger.error(f"Failed to initialize AsyncGroq client: {e}")

    async def chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 1500
    ) -> Dict[str, Any]:
        """
        Executes chat completion with retries and fallback models.
        Handles malformed tool calls by retrying once, then falling back to plain text.
        """
        if not self.client:
            return {
                "role": "assistant",
                "content": "LLM client not configured. Falling back to deterministic agent rules."
            }

        # Try primary model first, then fallback model
        models_to_try = [self.primary_model, self.fallback_model]
        
        for attempt_model in models_to_try:
            try:
                kwargs: Dict[str, Any] = {
                    "model": attempt_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"

                response = await self.client.chat.completions.create(**kwargs)
                message = response.choices[0].message
                
                # Check for tool calls
                if message.tool_calls:
                    valid_tool_calls = []
                    has_malformed = False
                    
                    for tc in message.tool_calls:
                        try:
                            # Verify arguments are valid JSON
                            if isinstance(tc.function.arguments, str):
                                json.loads(tc.function.arguments)
                            valid_tool_calls.append(tc)
                        except json.JSONDecodeError:
                            has_malformed = True
                            logger.warning(f"Malformed tool call arguments: {tc.function.arguments}")
                            
                    if has_malformed:
                        logger.info("Retrying once with fix prompt for malformed tool call...")
                        retry_messages = list(messages)
                        retry_messages.append({
                            "role": "user",
                            "content": "Your previous tool call contained invalid JSON. Please return valid JSON arguments for your tool call."
                        })
                        retry_resp = await self.client.chat.completions.create(
                            model=attempt_model,
                            messages=retry_messages,
                            tools=tools,
                            tool_choice="auto",
                            temperature=0.1
                        )
                        retry_msg = retry_resp.choices[0].message
                        if retry_msg.tool_calls:
                            return {
                                "role": "assistant",
                                "content": retry_msg.content or "",
                                "tool_calls": retry_msg.tool_calls
                            }
                    
                    return {
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": valid_tool_calls
                    }
                    
                return {
                    "role": "assistant",
                    "content": message.content or ""
                }
                
            except RateLimitError as e:
                logger.warning(f"Groq RateLimit on {attempt_model}: {e}. Trying fallback model...")
                continue
            except APIError as e:
                logger.warning(f"Groq APIError on {attempt_model}: {e}. Trying fallback...")
                continue
            except Exception as e:
                logger.error(f"Unexpected error calling Groq with {attempt_model}: {e}")
                continue

        # If all models failed, return graceful plain-text fallback
        logger.error("All Groq models failed. Returning emergency response.")
        return {
            "role": "assistant",
            "content": "Emergency Diagnostic Mode: Automated triage recommends checking live metrics, connection pools, and rolling back recent deployments."
        }

# Global singleton LLM instance
llm = GroqLLM()
