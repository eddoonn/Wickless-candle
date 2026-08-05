"""Phase 2 constrained, uncertainty-aware experiment selection.

The optimiser learns only from observations produced under the current validation
profile. It ranks the existing bounded candidate surface; it never changes risk,
sessions, strategy execution, evaluator gates, or candidate permissions.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable

from autoresearch.evaluator import ALLOWED_PARAMETERS
from autoresearch.phase1_validation import policy_profile_sha256
from no_wick_research import NoWickConfig


MODEL_VERSION = "phase2-constrained-kernel-v1"
LOCKED_PARAMETERS = frozenset({"use_session", "session_start", "session_end"})


@dataclass(frozen=True)
class CandidatePoint:
    name: str
    parameters: dict[str, Any]
    category: str
    priority_rank: int
    original_index: int


@dataclass(frozen=True)
class Observation:
    run_id: str
    parameters: dict[str, Any]
    objective: tuple[float, ...]
    checks: dict[str, bool]
    passed: bool
    effect: str


@dataclass(frozen=True)
class CandidateScore:
    candidate: CandidatePoint
    selection: str
    acquisition: float
    feasibility_probability: float
    expected_improvement: float
    predicted_objective_percentile: float
    uncertainty: float
    novelty: float
    nearest_distance: float
    effective_local_observations: float
    evaluation_cost: float
    gate_probabilities: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidate"] = asdict(self.candidate)
        return payload


def phase2_settings(policy: dict[str, Any]) -> dict[str, Any] | None:
    value = policy.get("phase2_optimization")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("phase2_optimization must be an object")
    required = {
        "profile",
        "minimum_observations",
        "exploration_fraction",
        "neighbour_count",
        "kernel_bandwidth",
        "uncertainty_weight",
        "diversity_weight",
    }
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("phase2_optimization is missing: " + ", ".join(missing))
    exploration = float(value["exploration_fraction"])
    if not 0.20 <= exploration <= 0.50:
        raise ValueError("Phase 2 exploration_fraction must be between 0.20 and 0.50")
    if int(value["minimum_observations"]) < 4:
        raise ValueError("Phase 2 minimum_observations must be at least four")
    if int(value["neighbour_count"]) < 3:
        raise ValueError("Phase 2 neighbour_count must be at least three")
    if float(value["kernel_bandwidth"]) <= 0:
        raise ValueError("Phase 2 kernel_bandwidth must be positive")
    return value


def _objective_tuple(record: dict[str, Any], order: Iterable[str]) -> tuple[float, ...]:
    objective = record.get("objective", {})
    return tuple(float(objective[key]) for key in order)


def comparable_observations(
    records: Iterable[dict[str, Any]], policy: dict[str, Any]
) -> list[Observation]:
    """Return only current-release, exact-validation-profile observations."""

    profile_sha = policy_profile_sha256(policy)
    release = policy["production_baseline_sha"]
    order = policy["objective_order"]
    observations: list[Observation] = []
    for record in records:
        candidate = record.get("candidate", {})
        parameters = candidate.get("parameters")
        gates = record.get("acceptance_gates", {})
        checks = gates.get("checks")
        if not isinstance(parameters, dict) or not isinstance(checks, dict):
            continue
        if record.get("production_release_sha") not in {None, release}:
            continue
        if record.get("validation_profile_sha256") != profile_sha:
            continue
        try:
            objective = _objective_tuple(record, order)
        except (KeyError, TypeError, ValueError):
            continue
        observations.append(
            Observation(
                run_id=str(record.get("run_id", "unknown")),
                parameters=dict(parameters),
                objective=objective,
                checks={str(key): bool(value) for key, value in checks.items()},
                passed=bool(gates.get("passed")),
                effect=str(record.get("effect", "unknown")),
            )
        )
    return observations


def _parameter_keys(
    candidates: Iterable[CandidatePoint], observations: Iterable[Observation]
) -> tuple[str, ...]:
    keys: set[str] = set()
    for candidate in candidates:
        keys.update(candidate.parameters)
    for observation in observations:
        keys.update(observation.parameters)
    return tuple(sorted(keys - LOCKED_PARAMETERS))


def _value(parameters: dict[str, Any], key: str, defaults: NoWickConfig) -> Any:
    return parameters[key] if key in parameters else getattr(defaults, key)


def _component_distance(left: Any, right: Any, key: str) -> float:
    expected, lower_or_choices, upper = ALLOWED_PARAMETERS[key]
    if expected in {int, float}:
        width = float(upper) - float(lower_or_choices)
        if width <= 0:
            return 0.0
        return abs(float(left) - float(right)) / width
    return 0.0 if left == right else 1.0


def parameter_distance(
    left: dict[str, Any],
    right: dict[str, Any],
    keys: Iterable[str],
) -> float:
    defaults = NoWickConfig()
    components = [
        _component_distance(_value(left, key, defaults), _value(right, key, defaults), key)
        for key in keys
    ]
    if not components:
        return 0.0
    return math.sqrt(sum(value * value for value in components) / len(components))


def _empirical_percentile(
    target: tuple[float, ...], objectives: list[tuple[float, ...]]
) -> float:
    if not objectives:
        return 0.5
    below = sum(value < target for value in objectives)
    equal = sum(value == target for value in objectives)
    return (below + 0.5 * equal) / len(objectives)


def _normal_expected_improvement(mean: float, deviation: float, incumbent: float) -> float:
    if deviation <= 1e-12:
        return max(0.0, mean - incumbent)
    difference = mean - incumbent
    z_score = difference / deviation
    density = math.exp(-0.5 * z_score * z_score) / math.sqrt(2.0 * math.pi)
    distribution = 0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0)))
    return max(0.0, difference * distribution + deviation * density)


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    total = sum(weights)
    return sum(value * weight for value, weight in zip(values, weights)) / total


def _weighted_stdev(values: list[float], weights: list[float], mean: float) -> float:
    total = sum(weights)
    variance = sum(
        weight * (value - mean) ** 2 for value, weight in zip(values, weights)
    ) / total
    return math.sqrt(max(0.0, variance))


def _score_candidate(
    candidate: CandidatePoint,
    *,
    observations: list[Observation],
    objectives: list[tuple[float, ...]],
    incumbent_objective: tuple[float, ...],
    keys: tuple[str, ...],
    settings: dict[str, Any],
) -> CandidateScore:
    distances = [
        (parameter_distance(candidate.parameters, row.parameters, keys), row)
        for row in observations
    ]
    distances.sort(key=lambda item: (item[0], item[1].run_id))
    neighbours = distances[: min(len(distances), int(settings["neighbour_count"]))]
    bandwidth = float(settings["kernel_bandwidth"])
    weights = [
        max(1e-9, math.exp(-0.5 * (distance / bandwidth) ** 2))
        for distance, _ in neighbours
    ]
    local = [row for _, row in neighbours]
    total_weight = sum(weights)
    effective = total_weight * total_weight / sum(weight * weight for weight in weights)

    gate_names = sorted({name for row in observations for name in row.checks})
    gate_probabilities: dict[str, float] = {}
    gate_uncertainties: list[float] = []
    for gate in gate_names:
        passed_weight = sum(
            weight * float(row.checks.get(gate, False))
            for weight, row in zip(weights, local)
        )
        failed_weight = total_weight - passed_weight
        alpha = 1.0 + passed_weight
        beta = 1.0 + failed_weight
        probability = alpha / (alpha + beta)
        variance = alpha * beta / ((alpha + beta) ** 2 * (alpha + beta + 1.0))
        gate_probabilities[gate] = probability
        gate_uncertainties.append(min(1.0, math.sqrt(variance) / 0.2886751346))
    feasibility = (
        math.exp(
            sum(math.log(max(1e-9, value)) for value in gate_probabilities.values())
            / len(gate_probabilities)
        )
        if gate_probabilities
        else 0.5
    )

    utilities = [_empirical_percentile(row.objective, objectives) for row in local]
    predicted_percentile = _weighted_mean(utilities, weights)
    local_deviation = _weighted_stdev(utilities, weights, predicted_percentile)
    incumbent_percentile = _empirical_percentile(incumbent_objective, objectives)
    expected_improvement = _normal_expected_improvement(
        predicted_percentile,
        max(local_deviation, 0.05 / math.sqrt(max(1.0, effective))),
        incumbent_percentile,
    )

    nearest = neighbours[0][0]
    novelty = min(1.0, nearest / max(0.05, bandwidth))
    gate_uncertainty = statistics.mean(gate_uncertainties) if gate_uncertainties else 1.0
    sample_uncertainty = 1.0 / math.sqrt(1.0 + effective)
    uncertainty = min(1.0, (gate_uncertainty + novelty + sample_uncertainty) / 3.0)
    priority_factor = 1.0 / (1.0 + 0.12 * max(0, candidate.priority_rank))
    evaluation_cost = 1.0 + 0.05 * max(0, len(candidate.parameters) - 1)
    acquisition = (
        feasibility
        * (0.20 + predicted_percentile * 0.25 + expected_improvement)
        * (1.0 + float(settings["uncertainty_weight"]) * uncertainty)
        * priority_factor
        / evaluation_cost
    )
    return CandidateScore(
        candidate=candidate,
        selection="unselected",
        acquisition=acquisition,
        feasibility_probability=feasibility,
        expected_improvement=expected_improvement,
        predicted_objective_percentile=predicted_percentile,
        uncertainty=uncertainty,
        novelty=novelty,
        nearest_distance=nearest,
        effective_local_observations=effective,
        evaluation_cost=evaluation_cost,
        gate_probabilities=gate_probabilities,
    )


def _distance_to_selected(
    score: CandidateScore,
    selected: list[CandidateScore],
    keys: tuple[str, ...],
) -> float:
    if not selected:
        return 1.0
    return min(
        parameter_distance(
            score.candidate.parameters,
            row.candidate.parameters,
            keys,
        )
        for row in selected
    )


def select_with_surrogate(
    candidates: list[CandidatePoint],
    records: list[dict[str, Any]],
    incumbent_objective: dict[str, Any],
    policy: dict[str, Any],
    batch_size: int,
) -> tuple[list[CandidateScore], dict[str, Any]]:
    """Select an exploitation/exploration batch from the bounded proposal pool."""

    settings = phase2_settings(policy)
    profile_sha = policy_profile_sha256(policy)
    if settings is None:
        return [], {
            "schema_version": 1,
            "model_version": MODEL_VERSION,
            "mode": "disabled",
            "profile_sha256": profile_sha,
            "observations": 0,
            "candidate_pool": len(candidates),
            "reason": "phase2_optimization is not configured",
        }
    observations = comparable_observations(records, policy)
    minimum = int(settings["minimum_observations"])
    diagnostics: dict[str, Any] = {
        "schema_version": 1,
        "model_version": MODEL_VERSION,
        "profile": settings["profile"],
        "profile_sha256": profile_sha,
        "observations": len(observations),
        "minimum_observations": minimum,
        "candidate_pool": len(candidates),
        "exploration_fraction": float(settings["exploration_fraction"]),
    }
    if len(observations) < minimum:
        diagnostics.update(
            {
                "mode": "diversified-fallback",
                "reason": (
                    f"need {minimum} exact-profile observations; "
                    f"found {len(observations)}"
                ),
                "exploit_count": 0,
                "explore_count": batch_size,
                "selected": [],
            }
        )
        return [], diagnostics
    if not candidates or batch_size < 1:
        diagnostics.update(
            {
                "mode": "constrained-surrogate",
                "reason": "candidate pool is empty",
                "exploit_count": 0,
                "explore_count": 0,
                "selected": [],
            }
        )
        return [], diagnostics

    order = policy["objective_order"]
    objectives = [row.objective for row in observations]
    incumbent_tuple = tuple(float(incumbent_objective[key]) for key in order)
    keys = _parameter_keys(candidates, observations)
    scored = [
        _score_candidate(
            candidate,
            observations=observations,
            objectives=objectives,
            incumbent_objective=incumbent_tuple,
            keys=keys,
            settings=settings,
        )
        for candidate in candidates
    ]

    requested = min(batch_size, len(scored))
    explore_count = max(1, int(math.ceil(requested * float(settings["exploration_fraction"]))))
    exploit_count = max(0, requested - explore_count)
    diversity = float(settings["diversity_weight"])
    remaining = list(scored)
    selected: list[CandidateScore] = []

    for _ in range(exploit_count):
        chosen = max(
            remaining,
            key=lambda row: (
                row.acquisition
                * (1.0 + diversity * _distance_to_selected(row, selected, keys)),
                row.feasibility_probability,
                row.expected_improvement,
                -row.candidate.original_index,
            ),
        )
        remaining.remove(chosen)
        selected.append(replace(chosen, selection="exploit"))

    for _ in range(min(explore_count, len(remaining))):
        chosen = max(
            remaining,
            key=lambda row: (
                row.uncertainty
                * (1.0 + diversity * _distance_to_selected(row, selected, keys)),
                row.novelty,
                row.feasibility_probability,
                -row.candidate.original_index,
            ),
        )
        remaining.remove(chosen)
        selected.append(replace(chosen, selection="explore"))

    diagnostics.update(
        {
            "mode": "constrained-surrogate",
            "reason": "exact-profile observation threshold reached",
            "feature_keys": list(keys),
            "exploit_count": sum(row.selection == "exploit" for row in selected),
            "explore_count": sum(row.selection == "explore" for row in selected),
            "selected": [row.to_dict() for row in selected],
            "training_run_ids_sha256": hashlib.sha256(
                "\n".join(sorted(row.run_id for row in observations)).encode("utf-8")
            ).hexdigest(),
        }
    )
    return selected, diagnostics
