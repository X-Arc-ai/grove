"""Pass-bound D9 read and judgment tools. Network calls finish before writer transactions."""

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from grove.api.errors import ApiError, invalid_arguments
from grove.api.internal import MAX_SMALL_BODY, Pass
from grove.api.uploads import _screener
from grove.api.views import read_json_body
from grove.models.belief_judge import judge_belief
from grove.models.errors import ModelError
from grove.models.layer import ModelLayer
from grove.models.verify_judge import JudgeInputError
from grove.oauth.metadata_document import DocumentBusy, DocumentRefused
from grove.runner.belief_read import fetch_retest

router = APIRouter(prefix="/v1/internal/brain")


def _call(request: Request, live, body: dict, *, retest: bool) -> dict:
    if live.claims.agent not in ("connector", "learner", "curator"):
        raise ApiError(403, "scope_denied", "this agent cannot request belief judgments")
    if retest and live.claims.agent != "connector":
        raise ApiError(403, "scope_denied", "only the Connector retests beliefs")
    driver, world_id = request.app.state.driver, live.world_id
    factory = getattr(request.app.state, "belief_model_factory", None) or ModelLayer
    read = None
    try:
        if retest:
            if set(body) != {"belief_id"} or not isinstance(body["belief_id"], str):
                raise JudgeInputError("a retest takes only belief_id")
            dependencies = getattr(request.app.state, "belief_read_dependencies", {})
            read = fetch_retest(
                driver,
                world_id,
                body["belief_id"],
                run_id=live.claims.run_id,
                screener=dependencies.get("screener") or _screener(),
                **{k: v for k, v in dependencies.items() if k != "screener"},
            )
            body = {
                "mode": "evidence",
                "belief_id": body["belief_id"],
                "source_id": read["source_id"],
                "locator": read["locator"],
            }
        with factory(driver) as layer:
            answer = judge_belief(layer, driver, world_id, body, run_id=live.claims.run_id)
    except JudgeInputError as error:
        raise invalid_arguments(str(error), "body") from None
    except (DocumentRefused, DocumentBusy):
        raise ApiError(
            503, "unavailable", "the authorized retest source could not be read"
        ) from None
    except ModelError as error:
        # Provider text can echo source data; the public error keeps only its fixed code.
        raise ApiError(
            error.status,
            error.code,
            "the belief judge could not complete",
            retryable=error.retryable,
        ) from None
    if read is not None:
        answer["read"] = read
    return answer


@router.post("/judge")
async def belief_judge(request: Request, live: Pass) -> dict:
    body = await read_json_body(request, MAX_SMALL_BODY)
    if not isinstance(body, dict):
        raise invalid_arguments("the judgment input must be an object", "body")
    return await run_in_threadpool(_call, request, live, body, retest=False)


@router.post("/retest")
async def belief_retest(request: Request, live: Pass) -> dict:
    body = await read_json_body(request, MAX_SMALL_BODY)
    if not isinstance(body, dict):
        raise invalid_arguments("the retest input must be an object", "body")
    return await run_in_threadpool(_call, request, live, body, retest=True)
