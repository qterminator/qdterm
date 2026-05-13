"""Main window for QTerminator."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QIcon, QKeySequence
from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QTabBar, QMessageBox, QWidget,
    QVBoxLayout, QLineEdit, QMenu,
)

from qterminator.terminal import TerminalWidget
from qterminator.splitter import SplitContainer
from qterminator.config import Config
from qterminator.translation import _ as tr


def _short_title(title):
    """Shorten a terminal title for tab display."""
    if len(title) > 30:
        return title[:27] + "..."
    return title


class EditableTabBar(QTabBar):
    """Tab bar with double-click rename and right-click context menu."""

    # Signals for the parent window to handle
    new_tab_requested = pyqtSignal()
    close_tab_requested = pyqtSignal(int)
    close_other_tabs_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._editor = None
        self._edited_index = -1
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def mouseDoubleClickEvent(self, event):
        idx = self.tabAt(event.pos())
        if idx >= 0:
            self._start_edit(idx)
        else:
            super().mouseDoubleClickEvent(event)

    def _start_edit(self, index):
        if self._editor:
            self._finish_edit()

        self._edited_index = index
        rect = self.tabRect(index)

        self._editor = QLineEdit(self)
        self._editor.setText(self.tabText(index))
        self._editor.setGeometry(rect)
        self._editor.selectAll()
        self._editor.setFocus()
        self._editor.show()
        self._editor.returnPressed.connect(self._finish_edit)
        self._editor.editingFinished.connect(self._finish_edit)

    def _finish_edit(self):
        if self._editor and self._edited_index >= 0:
            text = self._editor.text().strip()
            if text:
                self.setTabText(self._edited_index, text)
                # Store custom name so title updates don't overwrite it
                self.setTabData(self._edited_index, text)
            self._editor.deleteLater()
            self._editor = None
            self._edited_index = -1

    def _show_context_menu(self, pos):
        idx = self.tabAt(pos)
        menu = QMenu(self)

        new_tab = QAction(QIcon.fromTheme("tab-new"), "New Tab", menu)
        new_tab.triggered.connect(self.new_tab_requested.emit)
        menu.addAction(new_tab)

        menu.addSeparator()

        if idx >= 0:
            rename = QAction(QIcon.fromTheme("edit-rename"), "Rename Tab", menu)
            rename.triggered.connect(lambda: self._start_edit(idx))
            menu.addAction(rename)

            close = QAction(QIcon.fromTheme("tab-close"), "Close Tab", menu)
            close.triggered.connect(lambda: self.close_tab_requested.emit(idx))
            menu.addAction(close)

            if self.count() > 1:
                close_others = QAction(QIcon.fromTheme("edit-delete"), "Close Other Tabs", menu)
                close_others.triggered.connect(
                    lambda: self.close_other_tabs_requested.emit(idx)
                )
                menu.addAction(close_others)

        menu.exec(self.mapToGlobal(pos))


class MainWindow(QMainWindow):
    """Top-level window containing tabs of split terminal panes."""

    def __init__(self, parent=None, resolved_theme=None):
        super().__init__(parent)
        self.setWindowTitle("QTerminator")
        self._active_terminal = None
        self._zoomed_terminal = None
        self._zoom_hidden_widgets = []
        self._resolved_theme = resolved_theme or "dark"

        self._setup_tabs()
        self._setup_shortcuts()
        self._setup_menubar()
        self._setup_plugins()

        # Create initial tab with one terminal
        self.new_tab()

        self.resize(800, 500)

    # -- Setup --

    def _setup_tabs(self):
        self._tabs = QTabWidget()
        self._tab_bar = EditableTabBar(self._tabs)
        self._tabs.setTabBar(self._tab_bar)
        self._tabs.setTabsClosable(True)
        self._tabs.setMovable(True)
        self._tabs.setDocumentMode(True)
        self._tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._tab_bar.new_tab_requested.connect(self.new_tab)
        self._tab_bar.close_tab_requested.connect(self._on_tab_close_requested)
        self._tab_bar.close_other_tabs_requested.connect(self._close_other_tabs)
        self.setCentralWidget(self._tabs)
        self._update_tab_bar_visibility()

    def _setup_menubar(self):
        menubar = self.menuBar()

        # Hide by default; toggle with context menu or Ctrl+Shift+M
        config = Config()
        menubar.setVisible(config.get("general", "show_menubar", default=False))

        # File menu
        file_menu = menubar.addMenu(tr("&File"))
        file_menu.addAction(self._make_action(tr("New &Tab"), "Ctrl+Shift+T", self.new_tab, icon="tab-new"))
        file_menu.addAction(self._make_action(tr("New &Window"), "Ctrl+Shift+I", self._new_window, icon="window-new"))
        file_menu.addSeparator()
        file_menu.addAction(self._make_action(tr("&Close Terminal"), "Ctrl+Shift+W", self._close_active_terminal, icon="window-close"))
        file_menu.addAction(self._make_action(tr("&Quit"), "Ctrl+Shift+Q", self.close, icon="application-exit"))

        # Edit menu
        edit_menu = menubar.addMenu(tr("&Edit"))
        edit_menu.addAction(self._make_action(tr("&Copy"), "Ctrl+Shift+C", self._copy, icon="edit-copy"))
        edit_menu.addAction(self._make_action(tr("&Paste"), "Ctrl+Shift+V", self._paste, icon="edit-paste"))
        edit_menu.addSeparator()
        edit_menu.addAction(self._make_action(tr("&Search"), "Ctrl+Shift+F", self._search, icon="edit-find"))
        edit_menu.addAction(self._make_action(tr("&Reset"), "Ctrl+Shift+R", self._reset, icon="view-refresh"))
        edit_menu.addAction(self._make_action(tr("Reset && C&lear"), "Ctrl+Shift+G", self._reset_clear, icon="edit-clear"))
        edit_menu.addSeparator()
        edit_menu.addAction(self._make_action(tr("&Preferences..."), "", self._open_preferences, icon="preferences-system"))

        # View menu
        view_menu = menubar.addMenu(tr("&View"))
        view_menu.addAction(self._make_action(tr("Split &Horizontally"), "Ctrl+Shift+O", self._split_horizontal, icon="view-split-top-bottom"))
        view_menu.addAction(self._make_action(tr("Split &Vertically"), "Ctrl+Shift+E", self._split_vertical, icon="view-split-left-right"))
        view_menu.addAction(self._make_action(tr("&Rotate Splits"), "Meta+R", self._rotate_splits, icon="object-rotate-right"))
        view_menu.addSeparator()
        view_menu.addAction(self._make_action(tr("&Maximize Terminal"), "Ctrl+Shift+Z", self._toggle_zoom, icon="zoom-fit-best"))
        self._fullscreen_action = self._make_action(tr("&Full Screen"), "F11", self._toggle_fullscreen, icon="view-fullscreen")
        view_menu.addAction(self._fullscreen_action)
        view_menu.addSeparator()
        view_menu.addAction(self._make_action(tr("Zoom &In"), "Ctrl+Shift+=", self._zoom_in, icon="zoom-in"))
        view_menu.addAction(self._make_action(tr("Zoom &Out"), "Ctrl+Shift+-", self._zoom_out, icon="zoom-out"))
        view_menu.addAction(self._make_action(tr("Zoom &Normal"), "Ctrl+0", self._zoom_normal, icon="zoom-original"))
        view_menu.addSeparator()
        self._scrollbar_action = self._make_action(tr("Toggle &Scrollbar"), "Ctrl+Shift+S", self._toggle_scrollbar)
        self._scrollbar_action.setCheckable(True)
        self._scrollbar_action.setChecked(True)
        view_menu.addAction(self._scrollbar_action)

        # Terminal menu
        term_menu = menubar.addMenu(tr("&Terminal"))
        term_menu.addAction(self._make_action(tr("Edit &Terminal Title..."), "Ctrl+Alt+X", self._edit_terminal_title, icon="edit-rename"))
        term_menu.addAction(self._make_action(tr("Edit T&ab Title..."), "Ctrl+Alt+A", self._edit_tab_title, icon="edit-rename"))
        term_menu.addAction(self._make_action(tr("Edit &Window Title..."), "Ctrl+Alt+W", self._edit_window_title, icon="edit-rename"))
        term_menu.addSeparator()
        term_menu.addAction(self._make_action(tr("Read-&Only"), "", self._toggle_read_only, icon="object-locked"))

        # Broadcast submenu
        broadcast_menu = term_menu.addMenu(tr("&Broadcast Input"))
        broadcast_menu.setIcon(QIcon.fromTheme("network-wireless"))
        broadcast_menu.addAction(self._make_action(tr("&Off"), "", lambda: self._set_broadcast("off")))
        broadcast_menu.addAction(self._make_action(tr("&Group"), "", lambda: self._set_broadcast("group")))
        broadcast_menu.addAction(self._make_action(tr("&All"), "", lambda: self._set_broadcast("all")))

        # Tools menu — populated dynamically with plugin items grouped by category
        self._tools_menu = menubar.addMenu(tr("T&ools"))
        self._tools_menu.aboutToShow.connect(self._populate_tools_menu)

        # Help menu
        help_menu = menubar.addMenu(tr("&Help"))
        help_menu.addAction(self._make_action(tr("&About QTerminator"), "", self._show_about, icon="help-about"))

    def _populate_tools_menu(self):
        """Rebuild the Tools menu from plugin contributions on each open."""
        from qterminator.context_menu import (
            collect_plugin_items_by_category, CATEGORY_ORDER,
        )
        self._tools_menu.clear()
        if not self._active_terminal:
            placeholder = QAction("(no active terminal)", self._tools_menu)
            placeholder.setEnabled(False)
            self._tools_menu.addAction(placeholder)
            return
        by_cat = collect_plugin_items_by_category(self._active_terminal)
        if not by_cat:
            placeholder = QAction("(no plugin items)", self._tools_menu)
            placeholder.setEnabled(False)
            self._tools_menu.addAction(placeholder)
            return
        seen = set()
        for cat in CATEGORY_ORDER:
            if cat in by_cat:
                self._add_tools_submenu(cat, by_cat[cat])
                seen.add(cat)
        for cat in sorted(by_cat):
            if cat not in seen:
                self._add_tools_submenu(cat, by_cat[cat])

    def _add_tools_submenu(self, category, items):
        sub = QMenu(category, self._tools_menu)
        for label, callback in items:
            if label == "---":
                sub.addSeparator()
                continue
            act = QAction(label, sub)
            if callback is not None:
                act.triggered.connect(lambda checked=False, cb=callback: cb())
            sub.addAction(act)
        self._tools_menu.addMenu(sub)

    def _show_about(self):
        QMessageBox.about(
            self,
            "About QTerminator",
            "<h3>QTerminator 0.1.0</h3>"
            "<p>Qt port of Terminator terminal emulator</p>"
            "<p>Built with PyQt6 + QTermWidget</p>"
            "<p>License: GPL v3</p>"
        )

    def _setup_plugins(self):
        """Initialize the plugin manager and load enabled plugins."""
        from qterminator.plugin import PluginManager
        self._plugin_manager = PluginManager()
        self._plugin_manager.discover()
        # Auto-enable built-in plugins
        for name in self._plugin_manager.available_plugins():
            self._plugin_manager.enable(name, self)

    def _setup_shortcuts(self):
        """Register keyboard shortcuts that work even without menu bar focus."""
        # Tab navigation
        self._shortcut("Ctrl+PgUp", self._prev_tab)
        self._shortcut("Ctrl+PgDown", self._next_tab)
        self._shortcut("Ctrl+Tab", self._cycle_next)
        self._shortcut("Ctrl+Shift+Tab", self._cycle_prev)

        # Tab reordering
        self._shortcut("Ctrl+Shift+PgUp", self._move_tab_left)
        self._shortcut("Ctrl+Shift+PgDown", self._move_tab_right)

        # Switch to tab by number (Alt+1-9)
        for i in range(1, 10):
            self._shortcut(f"Alt+{i}", lambda idx=i-1: self._switch_to_tab(idx))

        # Terminal navigation in splits
        self._shortcut("Alt+Left", lambda: self._navigate("left"))
        self._shortcut("Alt+Right", lambda: self._navigate("right"))
        self._shortcut("Alt+Up", lambda: self._navigate("up"))
        self._shortcut("Alt+Down", lambda: self._navigate("down"))

        # Resize splits by keyboard
        self._shortcut("Ctrl+Shift+Right", lambda: self._resize_split("right"))
        self._shortcut("Ctrl+Shift+Left", lambda: self._resize_split("left"))
        self._shortcut("Ctrl+Shift+Up", lambda: self._resize_split("up"))
        self._shortcut("Ctrl+Shift+Down", lambda: self._resize_split("down"))

        # Scrollback navigation
        self._shortcut("Shift+PgUp", self._scroll_page_up)
        self._shortcut("Shift+PgDown", self._scroll_page_down)

        # Profile cycling
        self._shortcut("Ctrl+Alt+N", self._next_profile)
        self._shortcut("Ctrl+Alt+P", self._prev_profile)

        # Toggle menu bar
        self._shortcut("Ctrl+Shift+M", self._toggle_menubar)

    def _make_action(self, text, shortcut, slot, icon=None):
        if icon:
            action = QAction(QIcon.fromTheme(icon), text, self)
        else:
            action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        action.triggered.connect(slot)
        # Add to window so shortcuts work even when menubar is hidden
        self.addAction(action)
        return action

    def _shortcut(self, keys, slot):
        action = QAction(self)
        action.setShortcut(QKeySequence(keys))
        action.triggered.connect(slot)
        self.addAction(action)

    # -- Tab management --

    def new_tab(self, working_directory=None):
        """Create a new tab with a single terminal."""
        split = SplitContainer(Qt.Orientation.Horizontal)
        terminal = split.add_terminal(working_directory=working_directory)
        self._connect_terminal(terminal)

        idx = self._tabs.addTab(split, _short_title(terminal.title()))
        self._tabs.setCurrentIndex(idx)
        terminal.term.setFocus()
        self._set_active_terminal(terminal)
        self._update_tab_bar_visibility()

    def _on_tab_close_requested(self, index):
        split = self._tabs.widget(index)
        terminals = split.find_terminals()

        # Warn if any terminal has a running process
        if any(t.has_running_process() for t in terminals):
            reply = QMessageBox.question(
                self, "Close Tab",
                "A process is still running. Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._tabs.removeTab(index)
        split.deleteLater()
        self._update_tab_bar_visibility()

        if self._tabs.count() == 0:
            self.close()

    def _close_other_tabs(self, keep_index):
        """Close all tabs except the one at keep_index."""
        # Close from the end to avoid index shifting
        for i in range(self._tabs.count() - 1, -1, -1):
            if i != keep_index:
                split = self._tabs.widget(i)
                self._tabs.removeTab(i)
                split.deleteLater()
        self._update_tab_bar_visibility()

    def _on_tab_changed(self, index):
        if index < 0:
            return
        split = self._tabs.widget(index)
        if split:
            terminals = split.find_terminals()
            if terminals:
                terminals[0].term.setFocus()
                self._set_active_terminal(terminals[0])

    def _update_tab_bar_visibility(self):
        """Hide tab bar when there's only one tab."""
        self._tab_bar.setVisible(self._tabs.count() > 1)

    def _prev_tab(self):
        idx = self._tabs.currentIndex()
        if idx > 0:
            self._tabs.setCurrentIndex(idx - 1)

    def _next_tab(self):
        idx = self._tabs.currentIndex()
        if idx < self._tabs.count() - 1:
            self._tabs.setCurrentIndex(idx + 1)

    def _move_tab_left(self):
        idx = self._tabs.currentIndex()
        if idx > 0:
            self._tab_bar.moveTab(idx, idx - 1)

    def _move_tab_right(self):
        idx = self._tabs.currentIndex()
        if idx < self._tabs.count() - 1:
            self._tab_bar.moveTab(idx, idx + 1)

    # -- Terminal management --

    def _connect_terminal(self, terminal):
        """Connect a terminal's signals to window handlers."""
        terminal.title_changed.connect(self._on_terminal_title_changed)
        terminal.close_request.connect(self._on_terminal_close_request)
        terminal.focus_gained.connect(self._set_active_terminal)
        terminal.split_horizontal_request.connect(
            lambda t: self._split_at(t, Qt.Orientation.Vertical)
        )
        terminal.split_vertical_request.connect(
            lambda t: self._split_at(t, Qt.Orientation.Horizontal)
        )
        terminal.new_tab_request.connect(self.new_tab)
        terminal.term.termKeyPressed.connect(
            lambda event, t=terminal: self._on_terminal_key(t, event)
        )

    def _on_terminal_key(self, source_terminal, event):
        """Forward key events to broadcast targets."""
        if source_terminal is not self._active_terminal:
            return
        targets = self._get_broadcast_targets()
        for target in targets:
            if not target.is_read_only():
                target.term.sendKeyEvent(event)

    def _set_active_terminal(self, terminal):
        # Deactivate previous
        if self._active_terminal and self._active_terminal is not terminal:
            self._active_terminal.set_active(False)
        self._active_terminal = terminal
        terminal.set_active(True)
        self.setWindowTitle(terminal.title())
        # Update tab title to match active terminal
        for i in range(self._tabs.count()):
            split = self._tabs.widget(i)
            if terminal in split.find_terminals():
                self._tabs.setTabText(i, terminal.title())
                break

    def _on_terminal_title_changed(self, title):
        terminal = self.sender()
        for i in range(self._tabs.count()):
            split = self._tabs.widget(i)
            if terminal in split.find_terminals():
                if terminal is self._active_terminal:
                    # Don't overwrite user-set custom tab name
                    custom = self._tab_bar.tabData(i)
                    if not custom:
                        self._tabs.setTabText(i, _short_title(title))
                break
        if terminal is self._active_terminal:
            self.setWindowTitle(title)

    def _on_terminal_close_request(self, terminal):
        self._remove_terminal(terminal)

    def _remove_terminal(self, terminal):
        """Remove a terminal, cleaning up splits and tabs as needed."""
        for i in range(self._tabs.count()):
            split = self._tabs.widget(i)
            terminals = split.find_terminals()
            if terminal in terminals:
                if len(terminals) == 1:
                    # Last terminal in tab — close the tab
                    self._tabs.removeTab(i)
                    split.deleteLater()
                    if self._tabs.count() == 0:
                        self.close()
                else:
                    split.remove_terminal(terminal)
                    # Focus next available terminal
                    remaining = split.find_terminals()
                    if remaining:
                        remaining[0].term.setFocus()
                        self._set_active_terminal(remaining[0])
                return

    def _close_active_terminal(self):
        if self._active_terminal:
            if self._active_terminal.has_running_process():
                reply = QMessageBox.question(
                    self, "Close Terminal",
                    "A process is still running. Close anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
            self._remove_terminal(self._active_terminal)

    # -- Splits --

    def _current_split(self):
        return self._tabs.currentWidget()

    def _split_at(self, terminal, orientation):
        """Split at a specific terminal."""
        split = self._find_parent_splitter(terminal)
        if split:
            new_term = split.split(terminal, orientation)
            if new_term:
                self._connect_terminal(new_term)
                new_term.term.setFocus()
                self._set_active_terminal(new_term)

    def _find_parent_splitter(self, terminal):
        """Find the SplitContainer that directly contains this terminal."""
        parent = terminal.parent()
        while parent:
            if isinstance(parent, SplitContainer):
                if parent.indexOf(terminal) != -1:
                    return parent
            parent = parent.parent() if hasattr(parent, 'parent') else None
        return self._current_split()

    def _split_horizontal(self):
        if self._active_terminal:
            self._split_at(self._active_terminal, Qt.Orientation.Vertical)

    def _split_vertical(self):
        if self._active_terminal:
            self._split_at(self._active_terminal, Qt.Orientation.Horizontal)

    # -- Navigation --

    def _navigate(self, direction):
        if not self._active_terminal:
            return
        split = self._current_split()
        if split:
            next_term = split.find_next_terminal(self._active_terminal, direction)
            if next_term:
                next_term.term.setFocus()
                self._set_active_terminal(next_term)

    # -- Clipboard --

    def _copy(self):
        if self._active_terminal:
            self._active_terminal.copy_clipboard()

    def _paste(self):
        if self._active_terminal:
            self._active_terminal.paste_clipboard()

    # -- Search --

    def _search(self):
        if self._active_terminal:
            self._active_terminal.toggle_search()

    # -- Zoom --

    def _toggle_zoom(self):
        """Toggle maximizing the active terminal to fill the tab."""
        if self._zoomed_terminal:
            self._unzoom()
        elif self._active_terminal:
            self._zoom()

    def _zoom(self):
        """Maximize active terminal, hiding all others in the current tab."""
        split = self._current_split()
        if not split:
            return
        terminals = split.find_terminals()
        if len(terminals) <= 1:
            return  # nothing to zoom

        self._zoomed_terminal = self._active_terminal
        self._zoom_hidden_widgets = []

        # Hide all other terminals in the current tab
        for term in terminals:
            if term is not self._active_terminal:
                term.setVisible(False)
                self._zoom_hidden_widgets.append(term)

    def _unzoom(self):
        """Restore all hidden terminals."""
        for widget in self._zoom_hidden_widgets:
            widget.setVisible(True)
        self._zoom_hidden_widgets = []
        self._zoomed_terminal = None

    @property
    def is_zoomed(self):
        return self._zoomed_terminal is not None

    def _zoom_in(self):
        if self._active_terminal:
            self._active_terminal.zoom_in()

    def _zoom_out(self):
        if self._active_terminal:
            self._active_terminal.zoom_out()

    def _zoom_normal(self):
        """Reset zoom to default font size."""
        if self._active_terminal:
            profile = Config().get_profile("default")
            self._active_terminal.set_font(
                profile["font_family"], profile["font_size"]
            )

    # -- Fullscreen --

    def _toggle_menubar(self):
        menubar = self.menuBar()
        # Use isHidden() instead of isVisible() so it works when window is not shown
        visible = menubar.isHidden()
        menubar.setVisible(visible)
        config = Config()
        config.set("general", "show_menubar", visible)
        config.save()

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    # -- Scrollbar --

    def _toggle_scrollbar(self):
        from QTermWidget import QTermWidget
        show = self._scrollbar_action.isChecked()
        pos = (QTermWidget.ScrollBarPosition.ScrollBarRight if show
               else QTermWidget.ScrollBarPosition.NoScrollBar)
        for i in range(self._tabs.count()):
            split = self._tabs.widget(i)
            for term in split.find_terminals():
                term.term.setScrollBarPosition(pos)

    # -- Title editing --

    def _edit_terminal_title(self):
        if not self._active_terminal:
            return
        from PyQt6.QtWidgets import QInputDialog
        title, ok = QInputDialog.getText(
            self, "Edit Terminal Title", "Title:",
            text=self._active_terminal.title(),
        )
        if ok and title.strip():
            self._active_terminal._titlebar.set_title(title.strip())

    def _edit_tab_title(self):
        idx = self._tabs.currentIndex()
        if idx >= 0:
            from PyQt6.QtWidgets import QInputDialog
            title, ok = QInputDialog.getText(
                self, "Edit Tab Title", "Title:",
                text=self._tabs.tabText(idx),
            )
            if ok and title.strip():
                self._tabs.setTabText(idx, title.strip())
                self._tab_bar.setTabData(idx, title.strip())

    def _edit_window_title(self):
        from PyQt6.QtWidgets import QInputDialog
        title, ok = QInputDialog.getText(
            self, "Edit Window Title", "Title:",
            text=self.windowTitle(),
        )
        if ok and title.strip():
            self.setWindowTitle(title.strip())

    # -- Tab switching --

    def _switch_to_tab(self, index):
        if 0 <= index < self._tabs.count():
            self._tabs.setCurrentIndex(index)

    def _cycle_next(self):
        """Cycle to next terminal across all splits in current tab."""
        split = self._current_split()
        if split:
            next_term = split.find_next_terminal(self._active_terminal, "right")
            if next_term:
                next_term.term.setFocus()
                self._set_active_terminal(next_term)

    def _cycle_prev(self):
        split = self._current_split()
        if split:
            prev_term = split.find_next_terminal(self._active_terminal, "left")
            if prev_term:
                prev_term.term.setFocus()
                self._set_active_terminal(prev_term)

    # -- Resize splits --

    def _resize_split(self, direction):
        """Resize the active terminal's split by keyboard."""
        if not self._active_terminal:
            return
        parent = self._active_terminal.parent()
        if not isinstance(parent, SplitContainer):
            return
        idx = parent.indexOf(self._active_terminal)
        if idx == -1:
            return
        sizes = parent.sizes()
        if len(sizes) < 2:
            return
        step = 20
        if direction in ("right", "down"):
            if idx < len(sizes) - 1:
                sizes[idx] += step
                sizes[idx + 1] -= step
        elif direction in ("left", "up"):
            if idx < len(sizes) - 1:
                sizes[idx] -= step
                sizes[idx + 1] += step
        # Clamp to minimum
        sizes = [max(s, 10) for s in sizes]
        parent.setSizes(sizes)

    # -- Scrollback navigation --

    def _scroll_page_up(self):
        if self._active_terminal:
            # Find the scrollbar in QTermWidget and scroll up
            sb = self._active_terminal.term.findChild(
                __import__('PyQt6.QtWidgets', fromlist=['QScrollBar']).QScrollBar
            )
            if sb:
                sb.setValue(sb.value() - sb.pageStep())

    def _scroll_page_down(self):
        if self._active_terminal:
            sb = self._active_terminal.term.findChild(
                __import__('PyQt6.QtWidgets', fromlist=['QScrollBar']).QScrollBar
            )
            if sb:
                sb.setValue(sb.value() + sb.pageStep())

    # -- Profile cycling --

    def _next_profile(self):
        if not self._active_terminal:
            return
        profiles = Config().list_profiles()
        if len(profiles) < 2:
            return
        current = self._active_terminal._profile_name
        try:
            idx = profiles.index(current)
            next_idx = (idx + 1) % len(profiles)
        except ValueError:
            next_idx = 0
        self._active_terminal.apply_profile(profiles[next_idx])

    def _prev_profile(self):
        if not self._active_terminal:
            return
        profiles = Config().list_profiles()
        if len(profiles) < 2:
            return
        current = self._active_terminal._profile_name
        try:
            idx = profiles.index(current)
            prev_idx = (idx - 1) % len(profiles)
        except ValueError:
            prev_idx = 0
        self._active_terminal.apply_profile(profiles[prev_idx])

    # -- Reset --

    def _reset(self):
        if self._active_terminal:
            self._active_terminal.reset()

    def _reset_clear(self):
        if self._active_terminal:
            self._active_terminal.reset_clear()

    # -- Read-only --

    def _toggle_read_only(self):
        if self._active_terminal:
            self._active_terminal.toggle_read_only()

    # -- Rotate splits --

    def _rotate_splits(self):
        """Rotate the orientation of the current split container."""
        split = self._current_split()
        if split and split.count() > 1:
            if split.orientation() == Qt.Orientation.Horizontal:
                split.setOrientation(Qt.Orientation.Vertical)
            else:
                split.setOrientation(Qt.Orientation.Horizontal)

    # -- Broadcast --

    def _set_broadcast(self, mode):
        """Set broadcast mode: 'off', 'group', or 'all'."""
        self._broadcast_mode = mode

    def _get_broadcast_targets(self):
        """Get terminals that should receive broadcast input."""
        mode = getattr(self, '_broadcast_mode', 'off')
        if mode == "off" or not self._active_terminal:
            return []
        if mode == "all":
            targets = []
            for i in range(self._tabs.count()):
                split = self._tabs.widget(i)
                for t in split.find_terminals():
                    if t is not self._active_terminal:
                        targets.append(t)
            return targets
        if mode == "group":
            group = self._active_terminal.group
            if not group:
                return []
            targets = []
            for i in range(self._tabs.count()):
                split = self._tabs.widget(i)
                for t in split.find_terminals():
                    if t is not self._active_terminal and t.group == group:
                        targets.append(t)
            return targets
        return []

    # -- Theme --

    def apply_color_scheme_to_all(self, scheme_name):
        """Set the color scheme on every terminal in every tab."""
        for i in range(self._tabs.count()):
            split = self._tabs.widget(i)
            for term in split.find_terminals():
                term.set_color_scheme(scheme_name)

    # -- Preferences --

    def _open_preferences(self):
        from qterminator.preferences import PreferencesDialog
        dlg = PreferencesDialog(self)
        dlg.exec()

    # -- New window --

    def _new_window(self):
        window = MainWindow()
        window.show()

    # -- Layout --

    def save_layout(self):
        """Save current layout and window state to config."""
        from qterminator.layout import serialize_layout
        config = Config()
        layout = serialize_layout(self._tabs)
        config.set("layouts", "last_session", layout)

        # Save window state
        config.set("general", "window_maximized", self.isMaximized())
        if not self.isMaximized() and not self.isFullScreen():
            geom = self.geometry()
            config.set("general", "window_width", geom.width())
            config.set("general", "window_height", geom.height())
            config.set("general", "window_x", geom.x())
            config.set("general", "window_y", geom.y())

        config.save()

    def restore_layout(self):
        """Restore layout from config. Returns True if restored."""
        from qterminator.layout import restore_layout
        config = Config()
        layout = config.get("layouts", "last_session")
        if layout and layout.get("tabs"):
            # Remove any existing tabs before restoring
            while self._tabs.count():
                widget = self._tabs.widget(0)
                self._tabs.removeTab(0)
                widget.deleteLater()
            restore_layout(self, layout)
            return True
        return False

    def restore_window_state(self):
        """Restore window geometry and maximized state."""
        config = Config()
        w = config.get("general", "window_width", default=800)
        h = config.get("general", "window_height", default=500)
        x = config.get("general", "window_x", default=None)
        y = config.get("general", "window_y", default=None)
        self.resize(w, h)
        if x is not None and y is not None:
            self.move(x, y)
        if config.get("general", "window_maximized", default=False):
            self.showMaximized()

    # -- Close --

    def closeEvent(self, event):
        # Save layout before closing
        self.save_layout()

        # Check all terminals for running processes
        for i in range(self._tabs.count()):
            split = self._tabs.widget(i)
            for terminal in split.find_terminals():
                if terminal.has_running_process():
                    reply = QMessageBox.question(
                        self, "Quit QTerminator",
                        "There are still processes running. Quit anyway?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        event.ignore()
                        return
                    else:
                        event.accept()
                        return
        event.accept()
