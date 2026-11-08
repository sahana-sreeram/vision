"""Compare forest settings using training sessions only; never deploy a model."""
import argparse
import json
from pathlib import Path
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report
from sklearn.model_selection import GroupKFold, cross_val_predict
from .train_cursor_classifier import load_sessions
from .cursor_features import LABELS
from .collect_cursor_data import DATA_DIR


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exclude-session', action='append', default=[])
    args = parser.parse_args()
    paths = [p for p in sorted(DATA_DIR.glob('*.csv')) if p.name not in args.exclude_session]
    x, y, groups = load_sessions(paths)
    if len(set(groups)) < 3:
        parser.error('Need at least three training sessions')
    results = []
    for depth in (6, 10, None):
        for leaf in (2, 5, 12):
            model = RandomForestClassifier(n_estimators=150, max_depth=depth,
                                           min_samples_leaf=leaf, class_weight='balanced',
                                           random_state=42, n_jobs=1)
            predictions = cross_val_predict(model, x, y, groups=groups,
                                            cv=GroupKFold(n_splits=min(5, len(set(groups)))))
            report = classification_report(y, predictions, labels=list(LABELS), output_dict=True, zero_division=0)
            minimum = min(report[label][metric] for label in LABELS for metric in ('precision', 'recall'))
            results.append({'max_depth': depth, 'min_samples_leaf': leaf,
                            'minimum_class_precision_or_recall': minimum, 'report': report})
    results.sort(key=lambda r: r['minimum_class_precision_or_recall'], reverse=True)
    output = Path(__file__).parent / 'models' / 'tuning_report.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({'sessions': sorted(set(groups)), 'results': results,
                                  'note': 'Model selection scores, not independent final evaluation.'}, indent=2))
    best = results[0]
    print(json.dumps(best, indent=2))
    print(f'Saved {output}. Production model unchanged.')


if __name__ == '__main__':
    main()
