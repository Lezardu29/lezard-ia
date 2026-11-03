"""Multi-agents CrewAI pour les gros modèles (70 milliards de paramètres ou plus d'après le nom, ou modèle cloud de
pointe), et seulement pour les demandes complexes : une demande plus simple va à ce modèle seul (core.select).

Facultatif : sans le paquet crewai (requirements-multi.txt), ou pour un modèle plus petit, c'est l'orchestration de
orchestrator.py qui sert. Découpage et rôles viennent de orchestrator.plan ; ici chaque partie est confiée à un agent
CrewAI (son rôle, son objectif), les agents travaillent EN PARALLÈLE sur les IA de la répartition de l'orchestrateur
(assign : plusieurs fournisseurs, une remplaçante chacun), puis le gros modèle rédige la réponse finale. Chaque appel
de modèle passe par orchestrator._call : mêmes limites, mêmes fournisseurs, même suivi des coûts. En cas d'échec de
CrewAI, orchestrator.run prend le relais. La télémétrie de CrewAI est coupée avant son import.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from . import orchestrator
from .providers.base import ProviderError
from .registry import _PARAMS, ModelInfo

if TYPE_CHECKING:
    from .main import Gateway, Selection

# Avant tout import de crewai : aucune donnée envoyée à l'éditeur de CrewAI.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

log = logging.getLogger(__name__)
BIG_MODEL_B = 70.0
TIMEOUT_S = 900
STRATEGY = "crewai-parallel-assemble"
WORKER_BACKSTORY = (
    "Tu traites UNE partie d'une demande plus large, répartie entre plusieurs experts qui travaillent en parallèle. "
    "Demande complète, pour contexte seulement : « {question} ». Réponds de façon complète et précise à la seule "
    "partie qui t'est confiée, sans introduction ni conclusion générales. N'invente ni chiffres, ni lieux, ni "
    "sondages, ni sources : ce que la demande ne précise pas, présente-le comme une hypothèse à vérifier "
    "(« par exemple », « à adapter »)."
)
_llm_cls: type | None = None


def model_size_b(model_id: str) -> float | None:
    """Taille déduite du nom (« gpt-oss-120b » : 120). None si le nom n'en dit rien."""
    m = _PARAMS.search(model_id.replace(":", " ").replace("-", " "))
    return float(m.group(1)) if m else None


@lru_cache(maxsize=1)
def available() -> bool:
    """crewai est-il installé (requirements-multi.txt) ? Recherche seule : l'import (2,5 s) bloquerait la boucle."""
    try:
        return importlib.util.find_spec("crewai") is not None
    except (ImportError, ValueError):
        return False


def _no_dotenv() -> None:
    """CrewAI lit le fichier .env du dossier courant (python-dotenv) et en verse les valeurs dans l'environnement du
    processus : clé maître, licence, jetons se retrouvaient là où la passerelle ne les avait pas mis (constaté avec
    les dépendances de l'installateur). Ses appels de modèle passent tous par la passerelle : il n'en a pas besoin."""
    try:
        import dotenv
        import dotenv.main
    except ImportError:
        return

    def skip(*args: Any, **kwargs: Any) -> bool:
        return False

    dotenv.load_dotenv = dotenv.main.load_dotenv = skip


def _load() -> tuple:
    """Import de crewai, lourd : appelé dans un fil (asyncio.to_thread), jamais dans la boucle d'événements."""
    _no_dotenv()
    from crewai import Agent, Crew, Process, Task

    return Agent, Crew, Process, Task, _llm_class()


def is_big(info: ModelInfo) -> bool:
    """Gros modèle : au moins BIG_MODEL_B milliards de paramètres d'après son nom ; taille non publiée (Claude, GPT,
    Gemini…) : modèle cloud de pointe du catalogue (note de qualité d'au moins CREW_MIN_QUALITY)."""
    size = model_size_b(info.id)
    if size is not None:
        return size >= BIG_MODEL_B
    return info.hosting == "cloud" and info.quality >= float(os.getenv("CREW_MIN_QUALITY", "0.85"))


def worth_experts(complexity: str, info: ModelInfo) -> bool:
    """Routage auto : plusieurs experts pour une demande complexe. Gros modèle : seulement « high » (il répond seul au
    reste, mieux et moins cher) ; modèle plus petit (local) : dès « medium »."""
    return complexity == "high" or (complexity == "medium" and not is_big(info))


def applies(info: ModelInfo) -> bool:
    """Multi-agents CrewAI : crewai installé et gros modèle."""
    return is_big(info) and available()


def _llm_class() -> type:
    """Modèle de CrewAI qui n'appelle pas un fournisseur lui-même : chaque appel passe par la gateway."""
    global _llm_cls
    if _llm_cls is None:
        from crewai.llms.base_llm import BaseLLM

        class GatewayLLM(BaseLLM):
            gateway: Any = None
            selection: Any = None
            loop: Any = None  # boucle d'événements de la gateway (CrewAI tourne dans un autre fil)
            models: Any = None  # IA choisie, puis ses remplaçantes (orchestrator.assign)
            calls: Any = None  # suivi des appels : une entrée par tentative, réussie ou non
            part: int = 0

            def call(
                self,
                messages,
                tools=None,
                callbacks=None,
                available_functions=None,
                from_task=None,
                from_agent=None,
                response_model=None,
            ):
                if isinstance(messages, str):
                    messages = [{"role": "user", "content": messages}]
                body = {"messages": [{"role": m["role"], "content": m["content"]} for m in messages]}
                if self.part:  # expert (0 : l'assembleur) : réponse limitée, l'ensemble tient dans l'assemblage
                    body = orchestrator.capped(body)
                future = asyncio.run_coroutine_threadsafe(self._ask(body, from_agent), self.loop)
                return future.result(timeout=TIMEOUT_S)

            async def _ask(self, body: dict, from_agent) -> str:
                expert = str(getattr(from_agent, "role", "") or "")
                error: ProviderError | None = None
                for info in self.models:  # saturée ou en panne : la remplaçante prend la suite
                    try:
                        resp, ms = await orchestrator._call(self.gateway, self.selection, info, body)
                    except ProviderError as e:
                        self.calls.append(orchestrator._failed(info, "worker", e, part=self.part, expert=expert))
                        error = e
                        continue
                    usage = resp.get("usage") or {}
                    self.calls.append(orchestrator._agent(info, "worker", usage, ms, part=self.part, expert=expert))
                    return orchestrator._text(resp)
                raise error or ProviderError(502, "Aucune IA disponible pour cet agent.", "crewai")

        _llm_cls = GatewayLLM
    return _llm_cls


async def run(gw: Gateway, sel: Selection, body: dict, plan: orchestrator.Plan, t0: float) -> tuple[dict, dict]:
    """Même contrat que orchestrator.run : (réponse, métadonnées). Repli sur l'orchestrateur si CrewAI échoue."""
    try:
        return await _run(gw, sel, body, plan, t0)
    except Exception:
        log.warning("CrewAI n'a pas abouti : repli sur l'orchestrateur", exc_info=True)
        return await orchestrator.run(gw, sel, body, plan, t0)


async def _run(gw: Gateway, sel: Selection, body: dict, plan: orchestrator.Plan, t0: float) -> tuple[dict, dict]:
    Agent, Crew, Process, Task, llm_cls = await asyncio.to_thread(_load)
    loop = asyncio.get_running_loop()
    question = orchestrator._last_user_text(body["messages"])
    roles = [*plan.roles, *[""] * len(plan.parts)][: len(plan.parts)]
    jobs = orchestrator.assign(gw, sel, plan.parts)  # comme l'orchestrateur : parties réparties entre plusieurs IA

    async def ask_part(i: int, part: str, role: str, models: list[ModelInfo]) -> tuple[str | None, list[dict]]:
        llm = llm_cls(model=models[0].id, gateway=gw, selection=sel, loop=loop, models=models, calls=[], part=i + 1)
        agent = Agent(
            role=role or "assistant",
            goal=part,
            backstory=WORKER_BACKSTORY.format(question=question[:3000]),
            llm=llm,
            allow_delegation=False,
            max_retry_limit=0,  # les remplaçantes sont essayées par GatewayLLM : pas de nouvel essai de CrewAI
            verbose=False,
        )
        task = Task(
            description=part, expected_output="Réponse complète et précise à la seule partie confiée.", agent=agent
        )
        crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False, telemetry=False)
        try:
            out = await asyncio.to_thread(crew.kickoff)
        except Exception as e:
            return None, llm.calls or [orchestrator._failed(models[0], "worker", e, part=i + 1, expert=role)]
        return str(out.raw), llm.calls

    results = await asyncio.gather(*(ask_part(i, p, r, m) for i, (p, r, m) in enumerate(zip(plan.parts, roles, jobs))))
    answers = [a for a, _ in results]
    if not any(a is not None for a in answers):
        raise RuntimeError("aucune partie traitée par CrewAI")

    messages = orchestrator.assembler_messages(body["messages"], plan.parts, answers, roles)
    system, last = messages[0]["content"], messages[-1]["content"]
    if not isinstance(last, str):
        last = orchestrator._text_and_images(last)[0]
    asm_models = orchestrator.assembly_chain(sel)  # le gros modèle rédige ; saturé, le secours suivant (2.5)
    asm_llm = llm_cls(model=sel.info.id, gateway=gw, selection=sel, loop=loop, models=asm_models, calls=[], part=0)
    assembler = Agent(
        role="rédacteur de la réponse finale",
        goal="Répondre à l'utilisateur en assemblant les réponses des experts",
        backstory=system,
        llm=asm_llm,
        allow_delegation=False,
        max_retry_limit=0,
        verbose=False,
    )
    task = Task(
        description=last,
        expected_output="La réponse finale, directement destinée à l'utilisateur.",
        agent=assembler,
    )
    crew = Crew(agents=[assembler], tasks=[task], process=Process.sequential, verbose=False, telemetry=False)
    agents = [plan.planner, *[ag for _, ags in results for ag in ags]]
    try:
        out = await asyncio.to_thread(crew.kickoff)
        text, assembled = str(out.raw), True
    except Exception as e:
        text, assembled = orchestrator.concatenated(plan.parts, answers), False
        if not asm_llm.calls:  # échec avant tout appel de modèle
            asm_llm.calls.append(orchestrator._failed(sel.info, "assembler", e))
    agents += [{**c, "role": "assembler"} for c in asm_llm.calls]
    done = {c["ref"] for c in asm_llm.calls if c["ok"]}
    final = next((m for m in asm_models if m.ref in done), sel.info)  # l'IA qui a réellement rédigé
    usage, meta = orchestrator._meta(final, agents, plan.parts, t0, assembled)
    meta["orchestration"]["strategy"] = STRATEGY
    resp = orchestrator._fallback_response(final, text)
    resp["usage"] = usage
    return resp, meta
