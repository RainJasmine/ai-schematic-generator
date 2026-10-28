"""原型的石墨色与绿色主题；只使用 Qt Widgets，不引入浏览器运行时。"""

STYLESHEET = """
QMainWindow, QDialog, QWidget#shell { background: #101317; color: #e3e8ec; }
QWidget { color: #dce3e9; font-size: 13px; }
QFrame#sidebar { background: #15191e; border-right: 1px solid #2b323b; }
QFrame#contextPanel { background: #12161b; border-left: 1px solid #2b323b; }
QFrame#composer { background: #1b2128; border: 1px solid #3b4652; border-radius: 12px; }
QFrame#topbar { border-bottom: 1px solid #2b323b; }
QLabel#brand { font-size: 20px; font-weight: 600; color: #edf2f6; }
QLabel#brandMark { color: #bddc98; font-weight: 700; font-size: 18px; }
QLabel#pageHeading { font-size: 17px; font-weight: 600; color: #edf2f6; }
QLabel#eyebrow { color: #aecb94; font-size: 11px; }
QLabel#muted, QLabel#targetLabel { color: #95a5b5; }
QLabel#version { background: #252f25; color: #c1daa5; padding: 5px 10px; border-radius: 5px; }
QLabel#sectionTitle { color: #e3e8ec; font-weight: 600; }
QPushButton, QToolButton { background: #1b2128; border: 1px solid #333d49;
    border-radius: 6px; padding: 6px 9px; color: #d9e2e9; }
QPushButton:hover, QToolButton:hover { background: #27313b; border-color: #617353; }
QPushButton:focus, QToolButton:focus { border: 1px solid #b5d38e; }
QPushButton:disabled, QToolButton:disabled { color: #707d89; background: #171b20; border-color: #262e36; }
QPushButton[primary="true"] { background: #b6d491; border-color: #b6d491; color: #152014; font-weight: 600; }
QPushButton[primary="true"]:hover { background: #c6e2a4; }
QPushButton[primary="true"]:disabled { background: #343f2c; color: #8b9a7e; border-color: #343f2c; }
QPushButton[nav="true"] { text-align: left; background: transparent; border: 0; padding: 12px; }
QPushButton[nav="true"]:checked { background: #273124; color: #d2e8b9; }
QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox, QSpinBox, QDoubleSpinBox, QTableWidget {
    background: #191f26; color: #e3e8ec; border: 1px solid #35404b;
    border-radius: 6px; padding: 7px; selection-background-color: #43593c; selection-color: #f2f6ed; }
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus { border-color: #9ab77b; }
QLineEdit:disabled, QPlainTextEdit:disabled { color: #788592; }
QTextBrowser#conversation { background: #101317; border: none; padding: 8px; }
QPlainTextEdit#instruction { background: #1b2128; border: none; padding: 5px; }
QPlainTextEdit#eventLog { background: #11161b; font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12px; }
QGroupBox { border: 1px solid #2d3742; border-radius: 8px; margin-top: 14px; padding-top: 16px; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #b4c5d3; }
QTabWidget::pane { border: none; background: #101317; }
QTabBar::tab { background: #1b2128; padding: 7px 10px; border: 1px solid #303a45; }
QTabBar::tab:selected { color: #c2dda3; background: #273124; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: #151a20; width: 8px; margin: 0; }
QScrollBar::handle:vertical { background: #3a4651; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QProgressBar { background: #202832; border: none; border-radius: 3px; height: 6px; color: #a6b6c6; text-align: center; }
QProgressBar::chunk { background: #a9c98a; border-radius: 3px; }
QMenu, QComboBox QAbstractItemView { background: #202832; color: #e3e8ec; selection-background-color: #354b30; }
QToolTip { color: #e3e8ec; background: #26303a; border: 1px solid #526149; }
"""

STYLESHEET += "QHeaderView::section { background: #26303a; color: #dce5ee; padding: 6px; border: 1px solid #35404b; } QTableWidget { gridline-color: #35404b; }"
STYLESHEET += "QScrollBar:horizontal { background: #151a20; height: 8px; margin: 0; } QScrollBar::handle:horizontal { background: #3a4651; border-radius: 4px; min-width: 24px; } QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }"
