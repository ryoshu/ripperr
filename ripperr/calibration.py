"""Small, label-driven identity calibration helpers."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping, Sequence

from .models import Episode, SpeakerEmbedding, SpeakerName
from .store import _cosine


@dataclass(frozen=True)
class CalibrationSample:
    episode_guid: str
    speaker: str
    name: str
    margin: float
    target: int


@dataclass(frozen=True)
class CalibrationResult:
    identity: str
    episode_count: int
    sample_count: int
    positive_count: int
    negative_count: int
    intercept: float
    slope: float
    raw_accuracy: float
    leave_one_out_accuracy: float
    leave_one_out_brier: float

    def probability(self, margin: float) -> float:
        return _sigmoid(self.intercept + self.slope * margin)


def calibrate_identity(
    episodes: Sequence[Episode],
    names: Mapping[str, Sequence[SpeakerName]],
    embeddings: Mapping[str, Sequence[SpeakerEmbedding]],
    identity: str,
) -> CalibrationResult:
    """Fit a provisional identity probability from labeled episode samples.

    Each sample is scored against labeled samples from other episodes only. The
    feature is the best cosine score for the target identity minus the best score
    for every other identity. The model is intentionally recomputed from labels;
    no calibration state is persisted yet.
    """
    samples = _loo_samples(episodes, names, embeddings, identity)
    positives = sum(sample.target for sample in samples)
    negatives = len(samples) - positives
    if positives < 2 or negatives < 2:
        raise ValueError("calibration needs at least two positive and two negative samples")

    intercept, slope = _fit_logistic(samples)
    raw_accuracy = _accuracy(samples, lambda sample: sample.margin >= 0)

    predictions: list[tuple[int, float]] = []
    for guid in {sample.episode_guid for sample in samples}:
        training = [sample for sample in samples if sample.episode_guid != guid]
        testing = [sample for sample in samples if sample.episode_guid == guid]
        if not training or not testing:
            continue
        fold_intercept, fold_slope = _fit_logistic(training)
        predictions.extend(
            (sample.target, _sigmoid(fold_intercept + fold_slope * sample.margin))
            for sample in testing
        )

    loo_accuracy = sum((probability >= 0.5) == bool(target) for target, probability in predictions) / len(predictions)
    loo_brier = sum((probability - target) ** 2 for target, probability in predictions) / len(predictions)
    return CalibrationResult(
        identity=identity,
        episode_count=len({sample.episode_guid for sample in samples}),
        sample_count=len(samples),
        positive_count=positives,
        negative_count=negatives,
        intercept=intercept,
        slope=slope,
        raw_accuracy=raw_accuracy,
        leave_one_out_accuracy=loo_accuracy,
        leave_one_out_brier=loo_brier,
    )


def _loo_samples(
    episodes: Sequence[Episode],
    names: Mapping[str, Sequence[SpeakerName]],
    embeddings: Mapping[str, Sequence[SpeakerEmbedding]],
    identity: str,
) -> list[CalibrationSample]:
    identity_key = identity.casefold()
    labeled: dict[str, dict[str, str]] = {
        guid: {item.speaker: item.name for item in names.get(guid, ())}
        for guid in (episode.guid for episode in episodes)
    }
    vectors: dict[str, dict[str, tuple[float, ...]]] = {
        guid: {item.speaker: item.embedding for item in embeddings.get(guid, ())}
        for guid in labeled
    }
    samples: list[CalibrationSample] = []

    for episode in episodes:
        target_guid = episode.guid
        profiles: dict[str, list[tuple[float, ...]]] = defaultdict(list)
        for guid, labels in labeled.items():
            if guid == target_guid:
                continue
            for speaker, name in labels.items():
                vector = vectors[guid].get(speaker)
                if vector is not None:
                    profiles[name.casefold()].append(vector)

        identity_vectors = profiles.get(identity_key, [])
        other_vectors = [vector for key, values in profiles.items() if key != identity_key for vector in values]
        if not identity_vectors or not other_vectors:
            continue

        for speaker, name in labeled[target_guid].items():
            vector = vectors[target_guid].get(speaker)
            if vector is None:
                continue
            identity_score = max(_cosine(vector, sample) for sample in identity_vectors)
            other_score = max(_cosine(vector, sample) for sample in other_vectors)
            samples.append(
                CalibrationSample(
                    episode_guid=target_guid,
                    speaker=speaker,
                    name=name,
                    margin=identity_score - other_score,
                    target=int(name.casefold() == identity_key),
                )
            )
    return samples


def _fit_logistic(samples: Sequence[CalibrationSample]) -> tuple[float, float]:
    """Fit sigmoid(intercept + slope * margin) with a small ridge penalty."""
    positives = sum(sample.target for sample in samples)
    negatives = len(samples) - positives
    if not positives or not negatives:
        raise ValueError("logistic calibration needs both positive and negative samples")

    intercept = math.log(positives / negatives)
    slope = 1.0
    ridge = 1.0
    for _ in range(100):
        gradient_intercept = 0.0
        gradient_slope = -ridge * slope
        hessian_ii = 0.0
        hessian_is = 0.0
        hessian_ss = ridge
        for sample in samples:
            probability = _sigmoid(intercept + slope * sample.margin)
            weight = probability * (1 - probability)
            error = sample.target - probability
            gradient_intercept += error
            gradient_slope += error * sample.margin
            hessian_ii += weight
            hessian_is += weight * sample.margin
            hessian_ss += weight * sample.margin * sample.margin

        determinant = hessian_ii * hessian_ss - hessian_is * hessian_is
        if determinant <= 1e-12:
            break
        delta_intercept = (gradient_intercept * hessian_ss - gradient_slope * hessian_is) / determinant
        delta_slope = (gradient_slope * hessian_ii - gradient_intercept * hessian_is) / determinant
        intercept += delta_intercept
        slope += delta_slope
        if max(abs(delta_intercept), abs(delta_slope)) < 1e-8:
            break
    return intercept, slope


def _accuracy(samples: Sequence[CalibrationSample], classifier) -> float:
    return sum(classifier(sample) == bool(sample.target) for sample in samples) / len(samples)


def _sigmoid(value: float) -> float:
    if value >= 0:
        scaled = math.exp(-value)
        return 1 / (1 + scaled)
    scaled = math.exp(value)
    return scaled / (1 + scaled)
