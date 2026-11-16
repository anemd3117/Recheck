"""Render real Qt screens using explicitly labelled saved-record replay; no tool execution."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from recheck.engine import Engine
from recheck.gui import MainWindow
from recheck.models import load_settings, atomic_json
from recheck.ui_theme import STYLE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--probe')
    parser.add_argument('--result')
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    app = QApplication.instance() or QApplication([])
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    window = MainWindow(
        load_settings(),
        engine_factory=lambda s, on_event=None: Engine(
            s, data_dir=out / 'preview-data', on_event=on_event
        ),
        auto_check=False,
    )
    # Preview never writes the user's selected model or other saved preferences.
    window.persist_settings = lambda *_: True
    if args.probe:
        steps = json.loads(Path(args.probe).read_text(encoding='utf-8'))['steps']
        window.environment_done(steps['environment'])
        window.models_done(steps['models'])
        window.capabilities_done(steps['capabilities'])
    if args.result:
        record = json.loads(Path(args.result).read_text(encoding='utf-8'))
        window.show_result(record)
        if record.get('tests'):
            window.tests_editor.setPlainText(
                json.dumps(record['tests'], ensure_ascii=False, indent=2)
            )
        for target, card in [
            ('original', window.original),
            ('reimplementation', window.reimplementation),
        ]:
            card.path.setText(record.get('files', {}).get(target, {}).get('path', ''))
    window.show()
    captures = []
    for width, height in [(1280, 860), (960, 700)]:
        window.resize(width, height)
        for index, name in enumerate(
            ['start', 'compare', 'results', 'history', 'logs', 'settings']
        ):
            window.navigation.setCurrentRow(index)
            QTest.qWait(60)
            filename = f'{width}-{name}.png'
            window.grab().save(str(out / filename))
            captures.append(
                dict(file=filename, actual_size=[window.width(), window.height()], page=index)
            )
        window.navigation.setCurrentRow(2)
        window.result_tabs.setCurrentIndex(1)
        QTest.qWait(60)
        window.grab().save(str(out / f'{width}-ai-explanation.png'))
        window.result_tabs.setCurrentIndex(0)
    atomic_json(
        out / 'screens.json',
        dict(
            kind='Actual Qt rendering; saved integration record replay, not fresh integration',
            probe=args.probe,
            result=args.result,
            captures=captures,
            result_cards={key: card.value.text() for key, card in window.result_cards.items()},
            input_rows=window.input_panel.tree.topLevelItemCount(),
        ),
    )
    window.close()
    print(out)


if __name__ == '__main__':
    main()
