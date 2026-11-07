import logging
import asyncio
from typing import Dict, Any, List, Optional
from hindsight_client import Hindsight
from hindsight_client_api.exceptions import ApiException
from backend.config import settings

logger = logging.getLogger("echo_core.memory")

class IncidentMemory:
    """
    Thin wrapper over Hindsight client managing team memory banks (e.g. 'acme-sre').
    Implements retain_incident(), recall_for_alert(), and reflect_briefing().
    Handles network errors, bank initialization, and offline fallbacks.
    """
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        bank_id: Optional[str] = None
    ):
        self.base_url = (base_url or settings.HINDSIGHT_BASE_URL).rstrip("/")
        self.api_key = api_key or settings.HINDSIGHT_API_KEY
        self.bank_id = bank_id or settings.HINDSIGHT_BANK_ID
        
        self.is_healthy: bool = False
        self.last_error: Optional[str] = None
        self.client: Optional[Hindsight] = None
        
        self._init_client()

    def _init_client(self):
        try:
            if not self.api_key:
                self.is_healthy = False
                self.last_error = "HINDSIGHT_API_KEY not configured"
                logger.warning(self.last_error)
                return
                
            self.client = Hindsight(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=30.0
            )
            self.is_healthy = True
            self.last_error = None
        except Exception as e:
            self.is_healthy = False
            self.last_error = str(e)
            logger.error(f"Failed to initialize Hindsight client: {e}")

    async def ensure_bank(self, bank_id: Optional[str] = None) -> bool:
        """Ensures the memory bank exists, creating it if needed."""
        target_bank = bank_id or self.bank_id
        if not self.client:
            self.is_healthy = False
            return False

        try:
            # Try to create or verify bank
            await self.client.acreate_bank(
                bank_id=target_bank,
                name=f"{target_bank} On-Call Memory Bank",
                mission="Store, recall, and reflect on past production incidents, root causes, proven mitigations, and previously failed fixes."
            )
            self.is_healthy = True
            self.last_error = None
            logger.info(f"Verified Hindsight bank: {target_bank}")
            return True
        except ApiException as e:
            # 409 means bank already exists, which is healthy
            if e.status == 409:
                self.is_healthy = True
                self.last_error = None
                return True
            self.is_healthy = False
            self.last_error = f"Hindsight API error ({e.status}): {e.reason}"
            logger.error(self.last_error)
            return False
        except Exception as e:
            self.is_healthy = False
            self.last_error = f"Cannot reach Hindsight: {str(e)}"
            logger.error(self.last_error)
            return False

    async def retain_incident(
        self,
        incident: Dict[str, Any],
        feedback: Optional[Dict[str, Any]] = None,
        bank_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Retains an incident record into Hindsight, including symptoms, root cause,
        fix that worked, failed fixes, and engineer feedback.
        """
        target_bank = bank_id or self.bank_id
        if not self.client:
            return {"success": False, "error": "Hindsight client unavailable"}

        # Construct structured incident text for extraction
        svc = incident.get("svc", "unknown")
        inc_id = incident.get("id", "INC-UNKNOWN")
        title = incident.get("title", "")
        cause = incident.get("cause", "")
        sev = incident.get("sev", 3)
        resolver = incident.get("resolver", "On-Call SRE")
        fix_worked = incident.get("fix_worked", "")
        fixes_failed = incident.get("fixes_failed", "")
        if isinstance(fixes_failed, list):
            fixes_failed = "; ".join(fixes_failed)
            
        feedback_summary = ""
        if feedback:
            worked_str = "SUCCESS" if feedback.get("worked", True) else "FAILED"
            notes = feedback.get("notes", "")
            feedback_summary = f"\nEngineer Resolution Feedback: Fix outcome was {worked_str}. Notes: {notes}"

        content = (
            f"Incident Record {inc_id} on service '{svc}' (Severity: {sev}):\n"
            f"Alert Title: {title}\n"
            f"Suspected Root Cause: {cause}\n"
            f"Proven Fix That Worked: {fix_worked}\n"
            f"Fixes That FAILED (Do Not Repeat): {fixes_failed}\n"
            f"Resolved By: {resolver}{feedback_summary}\n"
        )

        try:
            response = await self.client.aretain(
                bank_id=target_bank,
                content=content,
                tags=[svc, inc_id, "incident-resolution"]
            )
            self.is_healthy = True
            logger.info(f"Retained incident {inc_id} into bank {target_bank}")
            return {
                "success": True,
                "incident_id": inc_id,
                "bank_id": target_bank,
                "response": str(response)
            }
        except Exception as e:
            self.is_healthy = False
            self.last_error = str(e)
            logger.error(f"Failed to retain incident {inc_id}: {e}")
            return {"success": False, "error": str(e)}

    async def recall_for_alert(
        self,
        service: str,
        error_signature: str,
        bank_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Recalls similar past incidents matching the service or error signature.
        """
        target_bank = bank_id or self.bank_id
        if not self.client:
            return []

        query = f"service: {service} error: {error_signature}"
        try:
            recall_resp = await self.client.arecall(
                bank_id=target_bank,
                query=query,
                budget="mid"
            )
            self.is_healthy = True
            results = []
            for item in getattr(recall_resp, "results", []):
                results.append({
                    "id": getattr(item, "id", ""),
                    "text": getattr(item, "text", ""),
                    "type": getattr(item, "type", ""),
                    "entities": getattr(item, "entities", []),
                    "scores": getattr(item, "scores", {}),
                    "tags": getattr(item, "tags", [])
                })
            return results
        except Exception as e:
            self.is_healthy = False
            self.last_error = str(e)
            logger.error(f"Hindsight recall failed for {service}: {e}")
            return []

    async def reflect_briefing(
        self,
        service: str,
        symptoms: str,
        error_log: str = "",
        bank_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Uses Hindsight reflect to synthesize a comprehensive incident briefing:
        - Likely cause
        - Fix that worked
        - Fixes that FAILED (Critical warning)
        - Historical resolver
        """
        target_bank = bank_id or self.bank_id
        if not self.client:
            return {
                "synthesized": False,
                "text": "Memory engine is offline.",
                "likely_cause": symptoms,
                "fix_worked": "Inspect service logs and standard runbook.",
                "fixes_failed": "No historical failure records available.",
                "resolver": "Unknown"
            }

        prompt = (
            f"Analyze incident for service '{service}'.\n"
            f"Observed Symptoms: {symptoms}\n"
            f"Logs: {error_log}\n\n"
            "Query past memories and answer with structured insight:\n"
            "1. What is the most likely root cause based on past patterns?\n"
            "2. What mitigation/fix worked in past incidents?\n"
            "3. CRITICAL: What fixes FAILED in the past and MUST NOT be attempted?\n"
            "4. Who previously resolved this or similar incidents?"
        )

        try:
            reflect_resp = await self.client.areflect(
                bank_id=target_bank,
                query=prompt,
                budget="low"
            )
            self.is_healthy = True
            reflect_text = getattr(reflect_resp, "text", "") or str(reflect_resp)
            return {
                "synthesized": True,
                "text": reflect_text,
                "based_on": getattr(reflect_resp, "based_on", None)
            }
        except Exception as e:
            self.is_healthy = False
            self.last_error = str(e)
            logger.error(f"Hindsight reflect failed: {e}")
            return {
                "synthesized": False,
                "error": str(e),
                "text": f"Memory reflection unavailable ({e}). Fallback to generic diagnostics."
            }

    async def get_stats(self, bank_id: Optional[str] = None) -> Dict[str, Any]:
        """Returns memory bank statistics and connection health."""
        target_bank = bank_id or self.bank_id
        return {
            "bank_id": target_bank,
            "base_url": self.base_url,
            "is_healthy": self.is_healthy,
            "last_error": self.last_error
        }

# Global singleton memory instance
memory = IncidentMemory()
