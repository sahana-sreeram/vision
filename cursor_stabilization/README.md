# Cursor stabilization

The cursor is smoothed with a One Euro filter. If a locally trained classifier is
present, it labels movement as STATIONARY, PRECISION or MOVING and blends the
filter settings toward a matching profile (`adaptive_smoothing.py`). Without a
model the app uses the baseline filter and shows UNTRAINED. Press `a` in the
camera window to toggle adaptation.

Recordings and trained models stay local; `data/` and `models/` are ignored by Git.
Only load joblib models you trust.

## Collect recordings

```sh
python -m cursor_stabilization.collect_cursor_data            # manual labels
python -m cursor_stabilization.collect_cursor_data --guided   # six timed exercises
python -m cursor_stabilization.collect_cursor_data --focus    # stationary vs precision only
```

In manual mode, with the camera window focused: `1` stationary, `2` precision,
`3` moving, `0` pause labelling, `q` finish. Keep the fingertip inside the purple
rectangle. Collect at least four sessions that cover all three states.

## Train

```sh
python -m cursor_stabilization.train_cursor_classifier --test-session SESSION.csv
python -m cursor_stabilization.tune_classifier          # compare forest settings
```

Reserve a full session for `--test-session`; at least three others are needed for
cross-validation. Metrics are saved next to the model in `models/`.

## Files

- `one_euro_filter.py`: the filter
- `adaptive_smoothing.py`: filter profiles per movement state
- `cursor_features.py`, `cursor_state.py`: motion features and live classification
- `collect_cursor_data.py`, `recording_app.py`: recording tool
- `train_cursor_classifier.py`, `tune_classifier.py`: training and tuning
