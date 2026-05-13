%Module(name=QTermWidget, use_limited_api=True)

%ModuleHeaderCode
#pragma GCC visibility push(default)
%End

%Import QtGui/QtGuimod.sip
%Import QtCore/QtCoremod.sip
%Import QtWidgets/QtWidgetsmod.sip

class QTermWidget : QWidget {

%TypeHeaderCode
#include <qtermwidget.h>
%End

public:
    enum ScrollBarPosition
    {
        NoScrollBar=0,
        ScrollBarLeft=1,
        ScrollBarRight=2
    };

    enum class KeyboardCursorShape
    {
        BlockCursor=0,
        UnderlineCursor=1,
        IBeamCursor=2
    };

    QTermWidget(int startnow = 1, QWidget *parent = 0);
    ~QTermWidget();
    void startTerminalTeletype();
    QSize sizeHint() const;
    void setTerminalSizeHint(bool on);
    bool terminalSizeHint();
    void startShellProgram();
    int getShellPID();
    int getForegroundProcessId();
    void changeDir(const QString & dir);
    void setTerminalFont(QFont &font);
    QFont getTerminalFont();
    void setTerminalOpacity(qreal level);
    void setEnvironment(const QStringList & environment);
    void setShellProgram(const QString & progname);
    void setWorkingDirectory(const QString & dir);
    QString workingDirectory();
    void setArgs(QStringList & args);
    void setColorScheme(const QString & name);
    QStringList getAvailableColorSchemes();
    static QStringList availableColorSchemes();
    static void addCustomColorSchemeDir(const QString& custom_dir);
    void setHistorySize(int lines);
    void setScrollBarPosition(ScrollBarPosition);
    void scrollToEnd();
    void sendText(QString &text);
    void setFlowControlEnabled(bool enabled);
    bool flowControlEnabled();
    void setFlowControlWarningEnabled(bool enabled);
    static QStringList availableKeyBindings();
    QString keyBindings();
    void setMotionAfterPasting(int);
    int historyLinesCount();
    int screenColumnsCount();
    void setSelectionStart(int row, int column);
    void setSelectionEnd(int row, int column);
    void getSelectionStart(int& row, int& column);
    void getSelectionEnd(int& row, int& column);
    QString selectedText(bool preserveLineBreaks = true);
    void setMonitorActivity(bool);
    void setMonitorSilence(bool);
    void setSilenceTimeout(int seconds);
    int getPtySlaveFd() const;
    void setKeyboardCursorShape(KeyboardCursorShape shape);
    void setAutoClose(bool);
    QString title() const;
    QString icon() const;
    void setBlinkingCursor(bool blink);
    void setBoldIntense(bool boldIntense);
    void setDrawLineChars(bool drawLineChars);
    int screenLinesCount();
    int historySize() const;
    void selectAll();
    void bracketText(QString &text);
    int getMargin() const;
    void setMargin(int);

    // Color table access (20 entries: 2 defaults + 8 normal + 8 intense + 2 unused)
    // Returns list of (r,g,b) tuples for all TABLE_COLORS entries
    SIP_PYOBJECT getColorTable();
%MethodCode
        #include "TerminalDisplay.h"
        // Access the internal TerminalDisplay via QTermWidget's layout
        Konsole::TerminalDisplay *display = sipCpp->findChild<Konsole::TerminalDisplay*>();
        if (display) {
            const ColorEntry *table = display->colorTable();
            PyObject *list = PyList_New(TABLE_COLORS);
            for (int i = 0; i < TABLE_COLORS; i++) {
                QColor c = table[i].color;
                PyObject *tuple = Py_BuildValue("(iii)", c.red(), c.green(), c.blue());
                PyList_SetItem(list, i, tuple);
            }
            sipRes = list;
        } else {
            sipRes = PyList_New(0);
        }
%End

    // Set a single color in the table by index and (r,g,b)
    void setColorEntry(int index, int r, int g, int b);
%MethodCode
        #include "TerminalDisplay.h"
        Konsole::TerminalDisplay *display = sipCpp->findChild<Konsole::TerminalDisplay*>();
        if (display && a0 >= 0 && a0 < TABLE_COLORS) {
            ColorEntry table[TABLE_COLORS];
            const ColorEntry *current = display->colorTable();
            for (int i = 0; i < TABLE_COLORS; i++)
                table[i] = current[i];
            table[a0].color = QColor(a1, a2, a3);
            display->setColorTable(table);
        }
%End
signals:
    void finished();
    void copyAvailable(bool);
    void termGetFocus();
    void termLostFocus();
    void termKeyPressed(QKeyEvent *);
    void urlActivated(const QUrl&, bool fromContextMenu);
    void bell(const QString& message);
    void activity();
    void silence();
    void sendData(const char *,int);
    void titleChanged();
    void receivedData(const QString &text);
    void profileChanged(const QString & profile);
public slots:
    void copyClipboard();
    void pasteClipboard();
    void pasteSelection();
    void zoomIn();
    void zoomOut();
    void setSize(const QSize &);
    void setKeyBindings(const QString & kb);
    void clear();
    void toggleShowSearchBar();
protected:
    virtual void resizeEvent(QResizeEvent *);
protected slots:
    void sessionFinished();
    void selectionChanged(bool textSelected);
private:
    void search(bool forwards, bool next);
    void setZoom(int step);
    void init(int startnow);
private slots:
    void find();
    void findNext();
    void findPrevious();
    void matchFound(int startColumn, int startLine, int endColumn, int endLine);
    void noMatchFound();
};
