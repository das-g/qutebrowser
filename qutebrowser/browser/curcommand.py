# Copyright 2014 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""The main tabbed browser widget."""

import os
import logging
import subprocess
from tempfile import mkstemp
from functools import partial

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import pyqtSlot, Qt, QObject, QProcess
from PyQt5.QtGui import QClipboard
from PyQt5.QtPrintSupport import QPrintDialog, QPrintPreviewDialog

import qutebrowser.utils.url as urlutils
import qutebrowser.utils.message as message
import qutebrowser.commands.utils as cmdutils
import qutebrowser.utils.webelem as webelem
import qutebrowser.config.config as config
import qutebrowser.browser.hints as hints
from qutebrowser.utils.misc import shell_escape
from qutebrowser.commands.exceptions import CommandError


class CurCommandDispatcher(QObject):

    """Command dispatcher for TabbedBrowser.

    Contains all commands which are related to the current tab.

    We can't simply add these commands to BrowserTab directly and use
    currentWidget() for TabbedBrowser.cur because at the time
    cmdutils.register() decorators are run, currentWidget() will return None.

    Attributes:
        _tabs: The TabbedBrowser object.
    """

    def __init__(self, parent):
        """Constructor.

        Args:
            parent: The TabbedBrowser for this dispatcher.
        """
        super().__init__(parent)
        self._tabs = parent

    def _scroll_percent(self, perc=None, count=None, orientation=None):
        """Inner logic for scroll_percent_(x|y).

        Args:
            perc: How many percent to scroll, or None
            count: How many percent to scroll, or None
            orientation: Qt.Horizontal or Qt.Vertical
        """
        if perc is None and count is None:
            perc = 100
        elif perc is None:
            perc = int(count)
        else:
            perc = float(perc)
        frame = self._tabs.currentWidget().page_.currentFrame()
        m = frame.scrollBarMaximum(orientation)
        if m == 0:
            return
        frame.setScrollBarValue(orientation, int(m * perc / 100))

    def _prevnext(self, prev, newtab):
        """Inner logic for {tab,}{prev,next}page."""
        widget = self._tabs.currentWidget()
        frame = widget.page_.currentFrame()
        if frame is None:
            raise CommandError("No frame focused!")
        widget.hintmanager.follow_prevnext(frame, widget.url(), prev, newtab)

    @cmdutils.register(instance='mainwindow.tabs.cur', name='open',
                       split=False)
    def openurl(self, url, count=None):
        """Open an url in the current/[count]th tab.

        Command handler for :open.

        Args:
            url: The URL to open.
            count: The tab index to open the URL in, or None.
        """
        tab = self._tabs.cntwidget(count)
        if tab is None:
            if count is None:
                # We want to open an URL in the current tab, but none exists
                # yet.
                self._tabs.tabopen(url)
            else:
                # Explicit count with a tab that doesn't exist.
                return
        else:
            tab.openurl(url)

    @pyqtSlot('QUrl', bool)
    def openurl_slot(self, url, newtab):
        """Open an URL, used as a slot.

        Args:
            url: The URL to open.
            newtab: True to open URL in a new tab, False otherwise.
        """
        if newtab:
            self._tabs.tabopen(url, background=False)
        else:
            self._tabs.currentWidget().openurl(url)

    @cmdutils.register(instance='mainwindow.tabs.cur', name='reload')
    def reloadpage(self, count=None):
        """Reload the current/[count]th tab.

        Command handler for :reload.

        Args:
            count: The tab index to reload, or None.
        """
        tab = self._tabs.cntwidget(count)
        if tab is not None:
            tab.reload()

    @cmdutils.register(instance='mainwindow.tabs.cur')
    def stop(self, count=None):
        """Stop loading in the current/[count]th tab.

        Command handler for :stop.

        Args:
            count: The tab index to stop, or None.
        """
        tab = self._tabs.cntwidget(count)
        if tab is not None:
            tab.stop()

    @cmdutils.register(instance='mainwindow.tabs.cur', name='printpreview')
    def printpreview(self, count=None):
        """Preview printing of the current/[count]th tab.

        Command handler for :printpreview.

        Args:
            count: The tab index to print, or None.
        """
        tab = self._tabs.cntwidget(count)
        if tab is not None:
            preview = QPrintPreviewDialog()
            preview.paintRequested.connect(tab.print)
            preview.exec_()

    @cmdutils.register(instance='mainwindow.tabs.cur', name='print')
    def printpage(self, count=None):
        """Print the current/[count]th tab.

        Command handler for :print.

        Args:
            count: The tab index to print, or None.
        """
        # QTBUG: We only get blank pages.
        # https://bugreports.qt-project.org/browse/QTBUG-19571
        # If this isn't fixed in Qt 5.3, bug should be reopened.
        tab = self._tabs.cntwidget(count)
        if tab is not None:
            printdiag = QPrintDialog()
            printdiag.open(lambda: tab.print(printdiag.printer()))

    @cmdutils.register(instance='mainwindow.tabs.cur')
    def back(self, count=1):
        """Go back in the history of the current tab.

        Command handler for :back.

        Args:
            count: How many pages to go back.
        """
        for _ in range(count):
            self._tabs.currentWidget().go_back()

    @cmdutils.register(instance='mainwindow.tabs.cur')
    def forward(self, count=1):
        """Go forward in the history of the current tab.

        Command handler for :forward.

        Args:
            count: How many pages to go forward.
        """
        for _ in range(count):
            self._tabs.currentWidget().go_forward()

    @cmdutils.register(instance='mainwindow.tabs.cur')
    def hint(self, groupstr='all', targetstr='normal'):
        """Start hinting.

        Command handler for :hint.

        Args:
            groupstr: The hinting mode to use.
            targetstr: Where to open the links.
        """
        widget = self._tabs.currentWidget()
        frame = widget.page_.mainFrame()
        if frame is None:
            raise CommandError("No frame focused!")
        try:
            group = getattr(webelem.Group, groupstr)
        except AttributeError:
            raise CommandError("Unknown hinting group {}!".format(groupstr))
        try:
            target = getattr(hints.Target, targetstr)
        except AttributeError:
            raise CommandError("Unknown hinting target {}!".format(targetstr))
        widget.hintmanager.start(frame, widget.url(), group, target)

    @cmdutils.register(instance='mainwindow.tabs.cur', hide=True)
    def follow_hint(self):
        """Follow the currently selected hint."""
        self._tabs.currentWidget().hintmanager.follow_hint()

    @pyqtSlot(str)
    def handle_hint_key(self, keystr):
        """Handle a new hint keypress."""
        self._tabs.currentWidget().hintmanager.handle_partial_key(keystr)

    @pyqtSlot(str)
    def fire_hint(self, keystr):
        """Fire a completed hint."""
        self._tabs.currentWidget().hintmanager.fire(keystr)

    @pyqtSlot(str)
    def filter_hints(self, filterstr):
        """Filter displayed hints."""
        self._tabs.currentWidget().hintmanager.filter_hints(filterstr)

    @cmdutils.register(instance='mainwindow.tabs.cur')
    def prevpage(self):
        """Open a "previous" link."""
        self._prevnext(prev=True, newtab=False)

    @cmdutils.register(instance='mainwindow.tabs.cur')
    def nextpage(self):
        """Open a "next" link."""
        self._prevnext(prev=False, newtab=False)

    @cmdutils.register(instance='mainwindow.tabs.cur')
    def tabprevpage(self):
        """Open a "previous" link in a new tab."""
        self._prevnext(prev=True, newtab=True)

    @cmdutils.register(instance='mainwindow.tabs.cur')
    def tabnextpage(self):
        """Open a "next" link in a new tab."""
        self._prevnext(prev=False, newtab=True)

    @pyqtSlot(str, int)
    def search(self, text, flags):
        """Search for text in the current page.

        Args:
            text: The text to search for.
            flags: The QWebPage::FindFlags.
        """
        self._tabs.currentWidget().findText(text, flags)

    @cmdutils.register(instance='mainwindow.tabs.cur', hide=True)
    def scroll(self, dx, dy, count=1):
        """Scroll the current tab by count * dx/dy.

        Command handler for :scroll.

        Args:
            dx: How much to scroll in x-direction.
            dy: How much to scroll in x-direction.
            count: multiplier
        """
        dx = int(int(count) * float(dx))
        dy = int(int(count) * float(dy))
        self._tabs.currentWidget().page_.currentFrame().scroll(dx, dy)

    @cmdutils.register(instance='mainwindow.tabs.cur', name='scroll_perc_x',
                       hide=True)
    def scroll_percent_x(self, perc=None, count=None):
        """Scroll the current tab to a specific percent of the page (horiz).

        Command handler for :scroll_perc_x.

        Args:
            perc: Percentage to scroll.
            count: Percentage to scroll.
        """
        self._scroll_percent(perc, count, Qt.Horizontal)

    @cmdutils.register(instance='mainwindow.tabs.cur', name='scroll_perc_y',
                       hide=True)
    def scroll_percent_y(self, perc=None, count=None):
        """Scroll the current tab to a specific percent of the page (vert).

        Command handler for :scroll_perc_y

        Args:
            perc: Percentage to scroll.
            count: Percentage to scroll.
        """
        self._scroll_percent(perc, count, Qt.Vertical)

    @cmdutils.register(instance='mainwindow.tabs.cur', hide=True)
    def scroll_page(self, mx, my, count=1):
        """Scroll the frame page-wise.

        Args:
            mx: How many pages to scroll to the right.
            my: How many pages to scroll down.
            count: multiplier
        """
        frame = self._tabs.currentWidget().page_.currentFrame()
        size = frame.geometry()
        frame.scroll(int(count) * float(mx) * size.width(),
                     int(count) * float(my) * size.height())

    @cmdutils.register(instance='mainwindow.tabs.cur')
    def yank(self, sel=False):
        """Yank the current url to the clipboard or primary selection.

        Command handler for :yank.

        Args:
            sel: True to use primary selection, False to use clipboard
        """
        clip = QApplication.clipboard()
        url = urlutils.urlstring(self._tabs.currentWidget().url())
        mode = QClipboard.Selection if sel else QClipboard.Clipboard
        clip.setText(url, mode)
        message.info("URL yanked to {}".format("primary selection" if sel
                                               else "clipboard"))

    @cmdutils.register(instance='mainwindow.tabs.cur', name='yanktitle')
    def yank_title(self, sel=False):
        """Yank the current title to the clipboard or primary selection.

        Command handler for :yanktitle.

        Args:
            sel: True to use primary selection, False to use clipboard
        """
        clip = QApplication.clipboard()
        title = self._tabs.tabText(self._tabs.currentIndex())
        mode = QClipboard.Selection if sel else QClipboard.Clipboard
        clip.setText(title, mode)
        message.info("Title yanked to {}".format("primary selection" if sel
                                                 else "clipboard"))

    @cmdutils.register(instance='mainwindow.tabs.cur', name='zoomin')
    def zoom_in(self, count=1):
        """Zoom in in the current tab.

        Args:
            count: How many steps to take.
        """
        tab = self._tabs.currentWidget()
        tab.zoom(count)

    @cmdutils.register(instance='mainwindow.tabs.cur', name='zoomout')
    def zoom_out(self, count=1):
        """Zoom out in the current tab.

        Args:
            count: How many steps to take.
        """
        tab = self._tabs.currentWidget()
        tab.zoom(-count)

    @cmdutils.register(instance='mainwindow.tabs.cur', name='zoom')
    def zoom_perc(self, zoom=None, count=None):
        """Zoom the current tab to [count] or 100 percent.

        Args:
            count: How many steps to take.
        """
        try:
            level = cmdutils.arg_or_count(zoom, count, default=100)
        except ValueError as e:
            raise CommandError(e)
        tab = self._tabs.currentWidget()
        tab.zoom_perc(level)

    @cmdutils.register(instance='mainwindow.tabs.cur', split=False)
    def spawn(self, cmd):
        """Spawn a command in a shell. {} gets replaced by the current URL.

        The URL will already be quoted correctly, so there's no need to do
        that.

        The command will be run in a shell, so you can use shell features like
        redirections.

        We use subprocess rather than Qt's QProcess here because of it's
        shell=True argument and because we really don't care about the process
        anymore as soon as it's spawned.

        Args:
            cmd: The command to execute.
        """
        url = urlutils.urlstring(self._tabs.currentWidget().url())
        cmd = cmd.replace('{}', shell_escape(url))
        logging.debug("Executing: {}".format(cmd))
        subprocess.Popen(cmd, shell=True)

    @cmdutils.register(instance='mainwindow.tabs.cur')
    def home(self):
        """Open main startpage in current tab."""
        self.openurl(config.get('general', 'startpage')[0])

    @cmdutils.register(instance='mainwindow.tabs.cur', modes=['insert'],
                       name='open_editor', hide=True)
    def editor(self):
        """Open an external editor with the current form field.

        We use QProcess rather than subprocess here because it makes it a lot
        easier to execute some code as soon as the process has been finished
        and do everything async.
        """
        frame = self._tabs.currentWidget().page_.currentFrame()
        elem = frame.findFirstElement(webelem.SELECTORS[
            webelem.Group.editable_focused])
        if elem.isNull():
            raise CommandError("No editable element focused!")
        oshandle, filename = mkstemp(text=True)
        text = elem.evaluateJavaScript('this.value')
        if text:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(text)
        proc = QProcess(self)
        proc.finished.connect(partial(self.on_editor_closed, elem, oshandle,
                                      filename))
        proc.error.connect(partial(self.on_editor_error, oshandle, filename))
        editor = config.get('general', 'editor')
        executable = editor[0]
        args = [arg.replace('{}', filename) for arg in editor[1:]]
        logging.debug("Calling '{}' with args {}".format(executable, args))
        proc.start(executable, args)

    def _editor_cleanup(self, oshandle, filename):
        """Clean up temporary file."""
        os.close(oshandle)
        try:
            os.remove(filename)
        except PermissionError:
            raise CommandError("Failed to delete tempfile...")

    def on_editor_closed(self, elem, oshandle, filename, exitcode,
                         exitstatus):
        """Write the editor text into the form field and clean up tempfile.

        Callback for QProcess when the editor was closed.
        """
        logging.debug("Editor closed")
        if exitcode != 0:
            raise CommandError("Editor did quit abnormally (status "
                               "{})!".format(exitcode))
        if exitstatus != QProcess.NormalExit:
            # No error here, since we already handle this in on_editor_error
            return
        if elem.isNull():
            raise CommandError("Element vanished while editing!")
        with open(filename, 'r', encoding='utf-8') as f:
            text = ''.join(f.readlines())
            text = webelem.javascript_escape(text)
        logging.debug("Read back: {}".format(text))
        elem.evaluateJavaScript("this.value='{}'".format(text))
        self._editor_cleanup(oshandle, filename)

    def on_editor_error(self, oshandle, filename, error):
        """Display an error message and clean up when editor crashed."""
        messages = {
            QProcess.FailedToStart: "The process failed to start.",
            QProcess.Crashed: "The process crashed.",
            QProcess.Timedout: "The last waitFor...() function timed out.",
            QProcess.WriteError: ("An error occurred when attempting to write "
                                  "to the process."),
            QProcess.ReadError: ("An error occurred when attempting to read "
                                 "from the process."),
            QProcess.UnknownError: "An unknown error occurred.",
        }
        self._editor_cleanup(oshandle, filename)
        raise CommandError("Error while calling editor: {}".format(
            messages[error]))
