"""Per-run cost accounting. cost_run(job_name) collects everything recorded inside the block and
saves one run_costs row, even if the block raises."""
import contextvars
from contextlib import contextmanager

import demo_mode
import pricing
import storage

_current = contextvars.ContextVar("current_run", default=None)


class RunCost:
    def __init__(self, job_name):
        """Start an empty cost record for one scheduler run."""
        self.job_name = job_name
        self.started_at = storage.utcnow()
        self.claude_usd = 0.0
        self.apify_usd = 0.0
        self.serper_usd = 0.0
        self.input_tokens = 0
        self.output_tokens = 0
        self.serper_credits = 0
        self.steps = {}
        self.counters = {}
        self.warnings = []

    def _step(self, name):
        """The per-step totals dict for `name`, created on first use."""
        return self.steps.setdefault(name, {"usd": 0.0, "calls": 0})


def current():
    """The cost run active in this context, or None."""
    return _current.get()


@contextmanager
def cost_run(job_name):
    # A nested cost_run joins the outer run so scheduler cycles produce exactly one record.
    """Collect all costs recorded inside the block and save one run_costs row, even if the block raises; nested runs join the
    outer one."""
    outer = _current.get()
    if outer is not None:
        yield outer
        return
    demo_mode.refuse(job_name)  # demo mode never records runs (the snapshot is read-only), so no job starts
    run = RunCost(job_name)
    token = _current.set(run)
    status = "success"
    try:
        yield run
    except Exception:
        status = "failed"
        raise
    finally:
        _current.reset(token)
        storage.save_run_cost({
            "job_name": run.job_name, "started_at": run.started_at, "finished_at": storage.utcnow(),
            "status": status, "claude_usd": run.claude_usd, "apify_usd": run.apify_usd,
            "serper_usd": run.serper_usd,
            "total_usd": run.claude_usd + run.apify_usd + run.serper_usd,
            "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
            "serper_credits": run.serper_credits,
            "details": {"steps": run.steps, "counters": run.counters, "warnings": run.warnings},
        })


def warn(message):
    """Attach a de-duplicated warning to the active run (ignored outside a run)."""
    run = _current.get()
    if run is not None and message not in run.warnings:
        run.warnings.append(message)


def incr(counter, amount=1):
    """Add to a named counter on the active run (ignored outside a run)."""
    run = _current.get()
    if run is not None:
        run.counters[counter] = run.counters.get(counter, 0) + amount


def record_claude(step, model, usage):
    """Record a Claude call's token usage and price it; an unknown model is tracked as unpriced, not guessed."""
    run = _current.get()
    if run is None:
        return
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cached = (getattr(usage, "cache_creation_input_tokens", 0) or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    if cached:
        warn("prompt-cache tokens present but not modeled in cost")
    cost = pricing.claude_cost(model, in_tok, out_tok)
    run.input_tokens += in_tok
    run.output_tokens += out_tok
    step = run._step(step)
    step["calls"] += 1
    step["input_tokens"] = step.get("input_tokens", 0) + in_tok
    step["output_tokens"] = step.get("output_tokens", 0) + out_tok
    if cost is None:
        warn(f"unpriced model: {model} (tokens tracked, cost not estimated)")
        step["unpriced"] = True
    else:
        run.claude_usd += cost
        step["usd"] += cost


def record_serper(credits, step="serper_corroboration"):
    """Record Serper credits and their cost against the active run."""
    run = _current.get()
    if run is None:
        return
    cost = pricing.serper_cost(credits)
    run.serper_credits += credits
    run.serper_usd += cost
    s = run._step(step)
    s["calls"] += 1
    s["usd"] += cost
    s["credits"] = s.get("credits", 0) + credits


def record_apify(run_id, usd, estimated, status):
    """Record one Apify run's cost (reported or estimated) and status against the active run."""
    run = _current.get()
    if run is None:
        return
    run.apify_usd += usd
    s = run._step("apify_news")
    s["calls"] += 1
    s["usd"] += usd
    s.setdefault("runs", []).append({"run_id": run_id, "usd": usd, "estimated": estimated, "status": status})
    if estimated:
        warn("apify cost estimated from result count (usage not reported)")
