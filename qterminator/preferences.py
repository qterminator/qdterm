"""Preferences dialog for QTerminator."""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import (
    QApplication, QDialog, QTabWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QSpinBox, QCheckBox, QDialogButtonBox, QLabel,
    QFontComboBox, QWidget, QGroupBox, QDoubleSpinBox, QPushButton,
    QFontDialog, QFrame,
)

from QTermWidget import QTermWidget

from qterminator.config import Config
from qterminator.translation import _ as tr


class PreferencesDialog(QDialog):
    """Application preferences dialog."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("QTerminator Preferences"))
        self.setMinimumSize(500, 400)
        self._config = Config()

        layout = QVBoxLayout(self)

        # Tab widget for categories
        self._tab_widget = QTabWidget()
        layout.addWidget(self._tab_widget)

        self._tab_widget.addTab(self._build_appearance_tab(), tr("Appearance"))
        self._tab_widget.addTab(self._build_behavior_tab(), tr("Behavior"))
        self._tab_widget.addTab(self._build_shortcuts_tab(), tr("Shortcuts"))

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._apply_and_close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._apply)
        layout.addWidget(buttons)

        self._load_current_settings()

    def _build_appearance_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Font group
        font_group = QGroupBox(tr("Font"))
        font_layout = QFormLayout(font_group)

        # Family combo + "Browse..." button to open full QFontDialog
        family_row = QHBoxLayout()
        self._font_combo = QFontComboBox()
        self._font_combo.setFontFilters(QFontComboBox.FontFilter.MonospacedFonts)
        self._font_combo.currentFontChanged.connect(self._update_font_preview)
        family_row.addWidget(self._font_combo, 1)
        browse_btn = QPushButton(tr("Browse..."))
        browse_btn.clicked.connect(self._browse_font)
        family_row.addWidget(browse_btn)
        font_layout.addRow(tr("Family:"), family_row)

        self._font_size = QSpinBox()
        self._font_size.setRange(6, 72)
        self._font_size.valueChanged.connect(self._update_font_preview)
        font_layout.addRow(tr("Size:"), self._font_size)

        self._font_ligatures = QCheckBox(tr("Enable ligatures (FiraCode, JetBrains Mono, Cascadia Code)"))
        self._font_ligatures.toggled.connect(self._update_font_preview)
        font_layout.addRow(self._font_ligatures)

        # Quick-select buttons for popular coding fonts (only show installed)
        popular_row = QHBoxLayout()
        popular_row.setSpacing(4)
        popular_label = QLabel(tr("Popular:"))
        popular_row.addWidget(popular_label)
        from PyQt6.QtGui import QFontDatabase
        installed = set(QFontDatabase.families())
        any_popular = False
        for name in ["Fira Code", "JetBrains Mono", "Cascadia Code",
                     "Hack", "Source Code Pro", "DejaVu Sans Mono",
                     "Iosevka", "Inconsolata"]:
            if name in installed:
                btn = QPushButton(name)
                btn.setFlat(True)
                btn.clicked.connect(lambda checked=False, n=name: self._set_font_family(n))
                popular_row.addWidget(btn)
                any_popular = True
        popular_row.addStretch()
        if any_popular:
            font_layout.addRow("", popular_row)

        # Live preview pane
        self._font_preview = QLabel()
        self._font_preview.setFrameStyle(QFrame.Shape.StyledPanel | QFrame.Shadow.Sunken)
        self._font_preview.setMinimumHeight(80)
        self._font_preview.setStyleSheet("background: #1e1e1e; color: #d3d7cf; padding: 8px;")
        self._font_preview.setText(
            "abcdefghijklmnopqrstuvwxyz 0123456789\n"
            "=> != >= <= -> ::  // /* */\n"
            "$ git commit -m 'fix: bug #42'"
        )
        font_layout.addRow(tr("Preview:"), self._font_preview)

        layout.addWidget(font_group)

        # Colors group
        color_group = QGroupBox(tr("Colors"))
        color_layout = QFormLayout(color_group)

        self._color_scheme = QComboBox()
        available_schemes = sorted(QTermWidget.availableColorSchemes())
        for scheme in available_schemes:
            self._color_scheme.addItem(scheme)
        color_layout.addRow(tr("Color Scheme:"), self._color_scheme)

        self._dark_color_scheme = QComboBox()
        for scheme in available_schemes:
            self._dark_color_scheme.addItem(scheme)
        color_layout.addRow(tr("Dark color scheme:"), self._dark_color_scheme)

        self._light_color_scheme = QComboBox()
        for scheme in available_schemes:
            self._light_color_scheme.addItem(scheme)
        color_layout.addRow(tr("Light color scheme:"), self._light_color_scheme)

        self._opacity = QDoubleSpinBox()
        self._opacity.setRange(0.0, 1.0)
        self._opacity.setSingleStep(0.05)
        self._opacity.setDecimals(2)
        color_layout.addRow(tr("Opacity:"), self._opacity)

        layout.addWidget(color_group)

        # Cursor group
        cursor_group = QGroupBox(tr("Cursor"))
        cursor_layout = QFormLayout(cursor_group)

        self._cursor_shape = QComboBox()
        self._cursor_shape.addItems(["Block", "Underline", "IBeam"])
        cursor_layout.addRow(tr("Shape:"), self._cursor_shape)

        self._cursor_blink = QCheckBox(tr("Blinking cursor"))
        cursor_layout.addRow(self._cursor_blink)

        layout.addWidget(cursor_group)
        layout.addStretch()

        return widget

    def _set_font_family(self, name):
        """Set font from a quick-select button."""
        self._font_combo.setCurrentFont(QFont(name))

    def _browse_font(self):
        """Open the full QFontDialog for advanced font selection."""
        current = QFont(
            self._font_combo.currentFont().family(),
            self._font_size.value(),
        )
        font, ok = QFontDialog.getFont(current, self, tr("Select Font"))
        if ok:
            self._font_combo.setCurrentFont(font)
            self._font_size.setValue(font.pointSize())

    def _update_font_preview(self):
        """Update the preview label to reflect current font selection."""
        font = QFont(
            self._font_combo.currentFont().family(),
            self._font_size.value(),
        )
        if self._font_ligatures.isChecked():
            font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
        self._font_preview.setFont(font)

    def _build_behavior_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Scrollback
        scroll_group = QGroupBox(tr("Scrollback"))
        scroll_layout = QFormLayout(scroll_group)

        self._scrollback = QSpinBox()
        self._scrollback.setRange(0, 1_000_000)
        self._scrollback.setSingleStep(1000)
        self._scrollback.setSpecialValueText(tr("Disabled"))
        scroll_layout.addRow(tr("Lines:"), self._scrollback)

        layout.addWidget(scroll_group)

        # Tabs
        tab_group = QGroupBox(tr("Tabs"))
        tab_layout = QFormLayout(tab_group)

        self._tab_position = QComboBox()
        self._tab_position.addItems(["Top", "Bottom", "Left", "Right"])
        tab_layout.addRow(tr("Tab position:"), self._tab_position)

        layout.addWidget(tab_group)

        # Window
        win_group = QGroupBox(tr("Window"))
        win_layout = QFormLayout(win_group)

        self._theme_mode = QComboBox()
        self._theme_mode.addItems(["System", "Dark", "Light"])
        win_layout.addRow(tr("Theme mode:"), self._theme_mode)

        self._confirm_close = QCheckBox(tr("Confirm before closing with running processes"))
        win_layout.addRow(self._confirm_close)

        self._show_menubar = QCheckBox(tr("Show menu bar"))
        win_layout.addRow(self._show_menubar)

        layout.addWidget(win_group)
        layout.addStretch()

        return widget

    def _build_shortcuts_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)

        info = QLabel(
            tr("Keyboard shortcuts are configured in\n~/.config/qterminator/config.toml\n\nDefault shortcuts:")
        )
        layout.addWidget(info)

        # Show current keybindings as a read-only list
        form = QFormLayout()
        keybindings = self._config.keybindings
        for action, keys in sorted(keybindings.items()):
            label = action.replace("_", " ").title()
            form.addRow(f"{label}:", QLabel(keys))
        layout.addLayout(form)
        layout.addStretch()

        return widget

    def _load_current_settings(self):
        profile = self._config.get_profile("default")

        # Appearance
        self._font_combo.setCurrentFont(QFont(profile["font_family"]))
        self._font_size.setValue(profile["font_size"])
        self._font_ligatures.setChecked(profile.get("font_ligatures", False))
        self._update_font_preview()

        scheme = profile["color_scheme"]
        idx = self._color_scheme.findText(scheme)
        if idx >= 0:
            self._color_scheme.setCurrentIndex(idx)

        self._opacity.setValue(profile.get("background_opacity", 1.0))

        cursor_map = {"block": 0, "underline": 1, "ibeam": 2}
        self._cursor_shape.setCurrentIndex(
            cursor_map.get(profile.get("cursor_shape", "block"), 0)
        )
        self._cursor_blink.setChecked(profile.get("cursor_blink", True))

        # Behavior
        self._scrollback.setValue(profile["scrollback_lines"])

        tab_pos = self._config.get("general", "tab_position", default="top")
        pos_map = {"top": 0, "bottom": 1, "left": 2, "right": 3}
        self._tab_position.setCurrentIndex(pos_map.get(tab_pos, 0))

        # Theme mode
        theme_mode = self._config.get("general", "theme_mode", default="system")
        mode_map = {"system": 0, "dark": 1, "light": 2}
        self._theme_mode.setCurrentIndex(mode_map.get(theme_mode, 0))

        # Dark/Light color schemes
        dark_scheme = self._config.get("general", "dark_color_scheme", default="Linux")
        idx = self._dark_color_scheme.findText(dark_scheme)
        if idx >= 0:
            self._dark_color_scheme.setCurrentIndex(idx)

        light_scheme = self._config.get("general", "light_color_scheme", default="BlackOnLightYellow")
        idx = self._light_color_scheme.findText(light_scheme)
        if idx >= 0:
            self._light_color_scheme.setCurrentIndex(idx)

        self._confirm_close.setChecked(
            self._config.get("general", "confirm_close", default=True)
        )
        self._show_menubar.setChecked(
            self._config.get("general", "show_menubar", default=False)
        )

    def _apply(self):
        """Apply settings to all open terminals and save to config."""
        parent = self.parent()
        if not parent:
            return

        font_family = self._font_combo.currentFont().family()
        font_size = self._font_size.value()
        color_scheme = self._color_scheme.currentText()
        scrollback = self._scrollback.value()
        cursor_shapes = ["block", "underline", "ibeam"]
        cursor_shape = cursor_shapes[self._cursor_shape.currentIndex()]
        cursor_blink = self._cursor_blink.isChecked()
        opacity = self._opacity.value()
        tab_positions = ["top", "bottom", "left", "right"]
        tab_pos = tab_positions[self._tab_position.currentIndex()]
        theme_modes = ["system", "dark", "light"]
        theme_mode = theme_modes[self._theme_mode.currentIndex()]
        dark_color_scheme = self._dark_color_scheme.currentText()
        light_color_scheme = self._light_color_scheme.currentText()

        # Save to config
        self._config.set("profiles", "default", "font_family", font_family)
        self._config.set("profiles", "default", "font_size", font_size)
        self._config.set("profiles", "default", "font_ligatures", self._font_ligatures.isChecked())
        self._config.set("profiles", "default", "color_scheme", color_scheme)
        self._config.set("profiles", "default", "scrollback_lines", scrollback)
        self._config.set("profiles", "default", "cursor_shape", cursor_shape)
        self._config.set("profiles", "default", "cursor_blink", cursor_blink)
        self._config.set("profiles", "default", "background_opacity", opacity)
        self._config.set("general", "tab_position", tab_pos)
        self._config.set("general", "confirm_close", self._confirm_close.isChecked())
        self._config.set("general", "theme_mode", theme_mode)
        self._config.set("general", "dark_color_scheme", dark_color_scheme)
        self._config.set("general", "light_color_scheme", light_color_scheme)
        show_menubar = self._show_menubar.isChecked()
        self._config.set("general", "show_menubar", show_menubar)
        self._config.save()

        # Apply theme
        from qterminator.theme import apply_theme, resolve_theme
        app = QApplication.instance()
        if app:
            resolved = apply_theme(app, theme_mode)
            if hasattr(parent, '_resolved_theme'):
                parent._resolved_theme = resolved

            # Apply the color scheme matching the resolved theme to terminals
            if hasattr(parent, 'apply_color_scheme_to_all'):
                if resolved == "light":
                    parent.apply_color_scheme_to_all(light_color_scheme)
                else:
                    parent.apply_color_scheme_to_all(dark_color_scheme)

        # Apply to all terminals in all tabs
        if hasattr(parent, '_tabs'):
            for i in range(parent._tabs.count()):
                split = parent._tabs.widget(i)
                for term in split.find_terminals():
                    term.set_font(font_family, font_size)
                    term.set_color_scheme(color_scheme)
                    term.set_scrollback(scrollback)

            # Apply tab position
            tab_pos_map = {
                0: QTabWidget.TabPosition.North,
                1: QTabWidget.TabPosition.South,
                2: QTabWidget.TabPosition.West,
                3: QTabWidget.TabPosition.East,
            }
            parent._tabs.setTabPosition(
                tab_pos_map.get(self._tab_position.currentIndex(),
                                QTabWidget.TabPosition.North)
            )

            # Apply menu bar visibility
            parent.menuBar().setVisible(show_menubar)

    def _apply_and_close(self):
        self._apply()
        self.accept()
