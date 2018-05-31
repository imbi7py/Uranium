# Copyright (c) 2018 Ultimaker B.V.
# Uranium is released under the terms of the LGPLv3 or higher.

import sys
import os
import signal
from typing import List
from typing import cast, Dict, Optional, TYPE_CHECKING

from PyQt5.QtCore import Qt, QCoreApplication, QEvent, QUrl, pyqtProperty, pyqtSignal, pyqtSlot, QLocale, QTranslator, QT_VERSION_STR, PYQT_VERSION_STR
from PyQt5.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlContext
from PyQt5.QtWidgets import QApplication, QSplashScreen, QMessageBox, QSystemTrayIcon
from PyQt5.QtGui import QIcon, QPixmap, QFontMetrics
from PyQt5.QtCore import QTimer

from UM.ConfigurationErrorMessage import ConfigurationErrorMessage
from UM.FileHandler.ReadFileJob import ReadFileJob
from UM.Mesh.MeshFileHandler import MeshFileHandler
from UM.Qt.Bindings.Theme import Theme
from UM.Workspace.WorkspaceFileHandler import WorkspaceFileHandler
from UM.Application import Application
from UM.Qt.QtRenderer import QtRenderer
from UM.Qt.Bindings.Bindings import Bindings
from UM.Signal import Signal, signalemitter
from UM.Resources import Resources
from UM.Logger import Logger
from UM.i18n import i18nCatalog
from UM.JobQueue import JobQueue
from UM.VersionUpgradeManager import VersionUpgradeManager
from UM.View.GL.OpenGLContext import OpenGLContext

from UM.Operations.GroupedOperation import GroupedOperation #To clear the scene.
from UM.Operations.RemoveSceneNodeOperation import RemoveSceneNodeOperation #To clear the scene.
from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator #To clear the scene.
from UM.Scene.SceneNode import SceneNode #To clear the scene.
from UM.Scene.Selection import Selection #To clear the selection after clearing the scene.

import UM.Settings.InstanceContainer  # For version upgrade to know the version number.
import UM.Settings.ContainerStack  # For version upgrade to know the version number.
import UM.Preferences  # For version upgrade to know the version number.
from UM.Mesh.ReadMeshJob import ReadMeshJob

import UM.Qt.Bindings.Theme
from UM.PluginRegistry import PluginRegistry
if TYPE_CHECKING:
    from PyQt5.QtCore import QObject


# Raised when we try to use an unsupported version of a dependency.
class UnsupportedVersionError(Exception):
    pass


# Check PyQt version, we only support 5.4 or higher.
major, minor = PYQT_VERSION_STR.split(".")[0:2]
if int(major) < 5 or int(minor) < 4:
    raise UnsupportedVersionError("This application requires at least PyQt 5.4.0")


##  Application subclass that provides a Qt application object.
@signalemitter
class QtApplication(QApplication, Application):
    pluginsLoaded = Signal()
    applicationRunning = Signal()
    
    def __init__(self, tray_icon_name: Optional[str] = None, **kwargs) -> None:
        plugin_path = ""
        if sys.platform == "win32":
            if hasattr(sys, "frozen"):
                plugin_path = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "PyQt5", "plugins")
                Logger.log("i", "Adding QT5 plugin path: %s", plugin_path)
                QCoreApplication.addLibraryPath(plugin_path)
            else:
                import site
                for sitepackage_dir in site.getsitepackages():
                    QCoreApplication.addLibraryPath(os.path.join(sitepackage_dir, "PyQt5", "plugins"))
        elif sys.platform == "darwin":
            plugin_path = os.path.join(self.getInstallPrefix(), "Resources", "plugins")

        if plugin_path:
            Logger.log("i", "Adding QT5 plugin path: %s", plugin_path)
            QCoreApplication.addLibraryPath(plugin_path)

        # use Qt Quick Scene Graph "basic" render loop
        os.environ["QSG_RENDER_LOOP"] = "basic"

        super().__init__(sys.argv, **kwargs) # type: ignore

        self._qml_import_paths = [] #type: List[str]
        self._main_qml = "main.qml"
        self._qml_engine = None
        self._main_window = None
        self._tray_icon_name = tray_icon_name
        self._tray_icon = None
        self._tray_icon_widget = None #type: Optional[QSystemTrayIcon]
        self._theme = None

        self._job_queue = None #type: Optional[JobQueue]
        self._version_upgrade_manager = None #type: Optional[VersionUpgradeManager]

        self._is_shutting_down = False

        self._recent_files = [] #type: List[str]

        self._configuration_error_message = None #type: Optional[ConfigurationErrorMessage]

    def addCommandLineOptions(self):
        super().addCommandLineOptions()
        # This flag is used by QApplication. We don't process it.
        self._cli_parser.add_argument("-qmljsdebugger",
                                      help = "For Qt's QML debugger compatibility")

    def initialize(self) -> None:
        super().initialize()

        self._mesh_file_handler = MeshFileHandler(self) #type: MeshFileHandler
        self._workspace_file_handler = WorkspaceFileHandler(self) #type: WorkspaceFileHandler

        self.setAttribute(Qt.AA_UseDesktopOpenGL)
        major_version, minor_version, profile = OpenGLContext.detectBestOpenGLVersion()

        if major_version is None and minor_version is None and profile is None:
            Logger.log("e", "Startup failed because OpenGL version probing has failed: tried to create a 2.0 and 4.1 context. Exiting")
            QMessageBox.critical(None, "Failed to probe OpenGL",
                                 "Could not probe OpenGL. This program requires OpenGL 2.0 or higher. Please check your video card drivers.")
            sys.exit(1)
        else:
            opengl_version_str = OpenGLContext.versionAsText(major_version, minor_version, profile)
            Logger.log("d", "Detected most suitable OpenGL context version: %s", opengl_version_str)
        OpenGLContext.setDefaultFormat(major_version, minor_version, profile = profile)

        self._qml_import_paths.append(os.path.join(os.path.dirname(sys.executable), "qml"))
        self._qml_import_paths.append(os.path.join(self.getInstallPrefix(), "Resources", "qml"))

        Logger.log("i", "Initializing job queue ...")
        self._job_queue = JobQueue()
        self._job_queue.jobFinished.connect(self._onJobFinished)

        Logger.log("i", "Initializing version upgrade manager ...")
        self._version_upgrade_manager = VersionUpgradeManager(self)

    def startSplashWindowPhase(self) -> None:
        super().startSplashWindowPhase()

        # Read preferences here (upgrade won't work) to get the language in use, so the splash window can be shown in
        # the correct language.
        try:
            preferences_filename = Resources.getPath(Resources.Preferences, self._app_name + ".cfg")
            self._preferences.readFromFile(preferences_filename)
        except FileNotFoundError:
            Logger.log("i", "Preferences file not found, ignore and use default language '%s'", self._default_language)

        signal.signal(signal.SIGINT, signal.SIG_DFL)
        # This is done here as a lot of plugins require a correct gl context. If you want to change the framework,
        # these checks need to be done in your <framework>Application.py class __init__().

        i18n_catalog = i18nCatalog("uranium")

        self._configuration_error_message = ConfigurationErrorMessage(self,
              i18n_catalog.i18nc("@info:status", "Your configuration seems to be corrupt."),
              lifetime = 0,
              title = i18n_catalog.i18nc("@info:title", "Configuration errors")
              )
        # Remove, install, and then loading plugins
        self.showSplashMessage(i18n_catalog.i18nc("@info:progress", "Loading plugins..."))
        # Remove and install the plugins that have been scheduled
        self._plugin_registry.initializeBeforePluginsAreLoaded()
        self._loadPlugins()
        self._plugin_registry.initializeAfterPluginsAreLoaded()
        self._plugin_registry.checkRequiredPlugins(self.getRequiredPlugins())
        self.pluginsLoaded.emit()

        self.showSplashMessage(i18n_catalog.i18nc("@info:progress", "Updating configuration..."))
        with self._container_registry.lockFile():
            VersionUpgradeManager.getInstance().upgrade()

        # Load preferences again because before we have loaded the plugins, we don't have the upgrade routine for
        # the preferences file. Now that we have, load the preferences file again so it can be upgraded and loaded.
        try:
            preferences_filename = Resources.getPath(Resources.Preferences, self._app_name + ".cfg")
            with open(preferences_filename, "r", encoding = "utf-8") as f:
                serialized = f.read()
            # This performs the upgrade for Preferences
            self._preferences.deserialize(serialized)
            self._preferences.setValue("general/plugins_to_remove", "")
            self._preferences.writeToFile(preferences_filename)
        except FileNotFoundError:
            Logger.log("i", "The preferences file cannot be found, will use default values")

        # Force the configuration file to be written again since the list of plugins to remove maybe changed
        self.showSplashMessage(i18n_catalog.i18nc("@info:progress", "Loading preferences..."))
        try:
            self._preferences_filename = Resources.getPath(Resources.Preferences, self._app_name + ".cfg")
            self._preferences.readFromFile(self._preferences_filename)
        except FileNotFoundError:
            Logger.log("i", "The preferences file '%s' cannot be found, will use default values",
                       self._preferences_filename)
            self._preferences_filename = Resources.getStoragePath(Resources.Preferences, self._app_name + ".cfg")

        # Preferences: recent files
        self._preferences.addPreference("%s/recent_files" % self._app_name, "")
        file_names = self._preferences.getValue("%s/recent_files" % self._app_name).split(";")
        for file_name in file_names:
            if not os.path.isfile(file_name):
                continue
            self._recent_files.append(QUrl.fromLocalFile(file_name))

        # Initialize System tray icon and make it invisible because it is used only to show pop up messages
        self._tray_icon = None
        if self._tray_icon_name:
            self._tray_icon = QIcon(Resources.getPath(Resources.Images, self._tray_icon_name))
            self._tray_icon_widget = QSystemTrayIcon(self._tray_icon)
            self._tray_icon_widget.setVisible(False)

    def initializeEngine(self):
        # TODO: Document native/qml import trickery
        self._qml_engine = QQmlApplicationEngine(self)
        self._qml_engine.setOutputWarningsToStandardError(False)
        self._qml_engine.warnings.connect(self.__onQmlWarning)

        for path in self._qml_import_paths:
            self._qml_engine.addImportPath(path)

        if not hasattr(sys, "frozen"):
            self._qml_engine.addImportPath(os.path.join(os.path.dirname(__file__), "qml"))

        self._qml_engine.rootContext().setContextProperty("QT_VERSION_STR", QT_VERSION_STR)
        self._qml_engine.rootContext().setContextProperty("screenScaleFactor", self._screenScaleFactor())

        self.registerObjects(self._qml_engine)

        Bindings.register()
        self._qml_engine.load(self._main_qml)
        self.engineCreatedSignal.emit()

    recentFilesChanged = pyqtSignal()

    @pyqtProperty("QVariantList", notify=recentFilesChanged)
    def recentFiles(self):
        return self._recent_files

    def _onJobFinished(self, job):
        if (not isinstance(job, ReadMeshJob) and not isinstance(job, ReadFileJob)) or not job.getResult():
            return

        f = QUrl.fromLocalFile(job.getFileName())
        if f in self._recent_files:
            self._recent_files.remove(f)

        self._recent_files.insert(0, f)
        if len(self._recent_files) > 10:
            del self._recent_files[10]

        pref = ""
        for path in self._recent_files:
            pref += path.toLocalFile() + ";"

        self.getPreferences().setValue("%s/recent_files" % self.getApplicationName(), pref)
        self.recentFilesChanged.emit()

    def run(self):
        pass

    def hideMessage(self, message):
        with self._message_lock:
            if message in self._visible_messages:
                message.hide(send_signal = False)  # we're in handling hideMessageSignal so we don't want to resend it
                self._visible_messages.remove(message)
                self.visibleMessageRemoved.emit(message)

    def showMessage(self, message):
        with self._message_lock:
            if message not in self._visible_messages:
                self._visible_messages.append(message)
                message.setLifetimeTimer(QTimer())
                message.setInactivityTimer(QTimer())
                self.visibleMessageAdded.emit(message)

        # also show toast message when the main window is minimized
        self.showToastMessage(self._app_name, message.getText())

    def _onMainWindowStateChanged(self, window_state):
        if self._tray_icon:
            visible = window_state == Qt.WindowMinimized
            self._tray_icon_widget.setVisible(visible)

    # Show toast message using System tray widget.
    def showToastMessage(self, title: str, message: str):
        if self.checkWindowMinimizedState() and self._tray_icon_widget:
            # NOTE: Qt 5.8 don't support custom icon for the system tray messages, but Qt 5.9 does.
            #       We should use the custom icon when we switch to Qt 5.9
            self._tray_icon_widget.showMessage(title, message)

    def setMainQml(self, path):
        self._main_qml = path

    def exec_(self, *args, **kwargs):
        self.applicationRunning.emit()
        super().exec_(*args, **kwargs)
        
    @pyqtSlot()
    def reloadQML(self):
        # only reload when it is a release build
        if not self.getIsDebugMode():
            return
        self._qml_engine.clearComponentCache()
        self._theme.reload()
        self._qml_engine.load(self._main_qml)
        # Hide the window. For some reason we can't close it yet. This needs to be done in the onComponentCompleted.
        for obj in self._qml_engine.rootObjects():
            if obj != self._qml_engine.rootObjects()[-1]:
                obj.hide()

    @pyqtSlot()
    def purgeWindows(self):
        # Close all root objects except the last one.
        # Should only be called by onComponentCompleted of the mainWindow.
        for obj in self._qml_engine.rootObjects():
            if obj != self._qml_engine.rootObjects()[-1]:
                obj.close()

    @pyqtSlot("QList<QQmlError>")
    def __onQmlWarning(self, warnings):
        for warning in warnings:
            Logger.log("w", warning.toString())

    engineCreatedSignal = Signal()

    def isShuttingDown(self):
        return self._is_shutting_down

    def registerObjects(self, engine):
        engine.rootContext().setContextProperty("PluginRegistry", PluginRegistry.getInstance())

    def getRenderer(self):
        if not self._renderer:
            self._renderer = QtRenderer()

        return self._renderer

    mainWindowChanged = Signal()

    def getMainWindow(self):
        return self._main_window

    def setMainWindow(self, window):
        if window != self._main_window:
            if self._main_window is not None:
                self._main_window.windowStateChanged.disconnect(self._onMainWindowStateChanged)

            self._main_window = window
            if self._main_window is not None:
                self._main_window.windowStateChanged.connect(self._onMainWindowStateChanged)

            self.mainWindowChanged.emit()

    def setVisible(self, visible):
        if self._main_window is not None:
            self._main_window.visible = visible

    @property
    def isVisible(self) -> bool:
        if self._main_window is not None:
            return self._main_window.visible
        return False

    def getTheme(self) -> Optional[Theme]:
        if self._theme is None:
            if self._qml_engine is None:
                Logger.log("e", "The theme cannot be accessed before the engine is initialised")
                return None

            self._theme = UM.Qt.Bindings.Theme.Theme.getInstance(self._qml_engine)
        return self._theme

    #   Handle a function that should be called later.
    def functionEvent(self, event):
        e = _QtFunctionEvent(event)
        QCoreApplication.postEvent(self, e)

    #   Handle Qt events
    def event(self, event):
        if event.type() == _QtFunctionEvent.QtFunctionEvent:
            event._function_event.call()
            return True

        return super().event(event)

    def windowClosed(self, save_data: bool = True) -> None:
        Logger.log("d", "Shutting down %s", self.getApplicationName())
        self._is_shutting_down = True

        if save_data:
            try:
                self.savePreferences()
            except Exception as e:
                Logger.log("e", "Exception while saving preferences: %s", repr(e))

        try:
            self.applicationShuttingDown.emit()
        except Exception as e:
            Logger.log("e", "Exception while emitting shutdown signal: %s", repr(e))

        try:
            self.getBackend().close()
        except Exception as e:
            Logger.log("e", "Exception while closing backend: %s", repr(e))

        self.quit()

    def checkWindowMinimizedState(self):
        if self._main_window is not None and self._main_window.windowState() == Qt.WindowMinimized:
            return True
        else:
            return False

    ##  Get the backend of the application (the program that does the heavy lifting).
    #   The backend is also a QObject, which can be used from qml.
    #   \returns Backend \type{Backend}
    @pyqtSlot(result = "QObject*")
    def getBackend(self):
        return self._backend

    ##  Property used to expose the backend
    #   It is made static as the backend is not supposed to change during runtime.
    #   This makes the connection between backend and QML more reliable than the pyqtSlot above.
    #   \returns Backend \type{Backend}
    @pyqtProperty("QVariant", constant = True)
    def backend(self):
        return self.getBackend()

    ##  Load a Qt translation catalog.
    #
    #   This method will locate, load and install a Qt message catalog that can be used
    #   by Qt's translation system, like qsTr() in QML files.
    #
    #   \param file_name The file name to load, without extension. It will be searched for in
    #                    the i18nLocation Resources directory. If it can not be found a warning
    #                    will be logged but no error will be thrown.
    #   \param language The language to load translations for. This can be any valid language code
    #                   or 'default' in which case the language is looked up based on system locale.
    #                   If the specified language can not be found, this method will fall back to
    #                   loading the english translations file.
    #
    #   \note When `language` is `default`, the language to load can be changed with the
    #         environment variable "LANGUAGE".
    def loadQtTranslation(self, file_name, language = "default"):
        # TODO Add support for specifying a language from preferences
        path = None
        if language == "default":
            path = self._getDefaultLanguage(file_name)
        else:
            path = Resources.getPath(Resources.i18n, language, "LC_MESSAGES", file_name + ".qm")

        # If all else fails, fall back to english.
        if not path:
            Logger.log("w", "Could not find any translations matching {0} for file {1}, falling back to english".format(language, file_name))
            try:
                path = Resources.getPath(Resources.i18n, "en_US", "LC_MESSAGES", file_name + ".qm")
            except FileNotFoundError:
                Logger.log("w", "Could not find English translations for file {0}. Switching to developer english.".format(file_name))
                return

        translator = QTranslator()
        if not translator.load(path):
            Logger.log("e", "Unable to load translations %s", file_name)
            return

        # Store a reference to the translator.
        # This prevents the translator from being destroyed before Qt has a chance to use it.
        self._translators[file_name] = translator

        # Finally, install the translator so Qt can use it.
        self.installTranslator(translator)

    ## Create a class variable so we can manage the splash in the CrashHandler dialog when the Application instance
    # is not yet created, e.g. when an error occurs during the initialization
    splash = None

    def createSplash(self):
        if not self.getIsHeadLess():
            try:
                QtApplication.splash = self._createSplashScreen()
            except FileNotFoundError:
                QtApplication.splash = None
            else:
                if QtApplication.splash:
                    QtApplication.splash.show()
                    self.processEvents()

    ##  Display text on the splash screen.
    def showSplashMessage(self, message):
        if not QtApplication.splash:
            self.createSplash()
        
        if QtApplication.splash:
            QtApplication.splash.showMessage(message, Qt.AlignHCenter | Qt.AlignVCenter)
            self.processEvents()
        elif self.getIsHeadLess():
            Logger.log("d", message)

    ##  Close the splash screen after the application has started.
    def closeSplash(self):
        if QtApplication.splash:
            QtApplication.splash.close()
            QtApplication.splash = None

    ## Create a QML component from a qml file.
    #  \param qml_file_path: The absolute file path to the root qml file.
    #  \param context_properties: Optional dictionary containing the properties that will be set on the context of the
    #                              qml instance before creation.
    #  \return None in case the creation failed (qml error), else it returns the qml instance.
    #  \note If the creation fails, this function will ensure any errors are logged to the logging service.
    def createQmlComponent(self, qml_file_path: str, context_properties: Dict[str, "QObject"]=None) -> Optional["QObject"]:
        if self._qml_engine is None: # Protect in case the engine was not initialized yet
            return None
        path = QUrl.fromLocalFile(qml_file_path)
        component = QQmlComponent(self._qml_engine, path)
        result_context = QQmlContext(self._qml_engine.rootContext())
        if context_properties is not None:
            for name, value in context_properties.items():
                result_context.setContextProperty(name, value)
        result = component.create(result_context)
        for err in component.errors():
            Logger.log("e", str(err.toString()))
        if result is None:
            return None

        # We need to store the context with the qml object, else the context gets garbage collected and the qml objects
        # no longer function correctly/application crashes.
        result.attached_context = result_context
        return result

    ##  Delete all nodes containing mesh data in the scene.
    #   \param only_selectable. Set this to False to delete objects from all build plates
    @pyqtSlot()
    def deleteAll(self, only_selectable = True) -> None:
        Logger.log("i", "Clearing scene")
        if not self.getController().getToolsEnabled():
            return

        nodes = []
        for node in DepthFirstIterator(self.getController().getScene().getRoot()): #type: ignore #Ignore type error because iter() should get called automatically by Python syntax.
            if not isinstance(node, SceneNode):
                continue
            if (not node.getMeshData() and not node.callDecoration("getLayerData")) and not node.callDecoration("isGroup"):
                continue  # Node that doesnt have a mesh and is not a group.
            if only_selectable and not node.isSelectable():
                continue
            if not node.callDecoration("isSliceable") and not node.callDecoration("getLayerData") and not node.callDecoration("isGroup"):
                continue  # Only remove nodes that are selectable.
            if node.getParent() and cast(SceneNode, node.getParent()).callDecoration("isGroup"):
                continue  # Grouped nodes don't need resetting as their parent (the group) is resetted)
            nodes.append(node)
        if nodes:
            op = GroupedOperation()

            for node in nodes:
                op.addOperation(RemoveSceneNodeOperation(node))

                # Reset the print information
                self.getController().getScene().sceneChanged.emit(node)

            op.push()
            Selection.clear()

    ##  Get the MeshFileHandler of this application.
    def getMeshFileHandler(self) -> MeshFileHandler:
        return self._mesh_file_handler

    def getWorkspaceFileHandler(self) -> WorkspaceFileHandler:
        return self._workspace_file_handler

    ##  Gets the instance of this application.
    #
    #   This is just to further specify the type of Application.getInstance().
    #   \return The instance of this application.
    @classmethod
    def getInstance(cls, *args, **kwargs) -> "QtApplication":
        return cast(QtApplication, super().getInstance(**kwargs))

    def _createSplashScreen(self):
        return QSplashScreen(QPixmap(Resources.getPath(Resources.Images, self.getApplicationName() + ".png")))

    def _screenScaleFactor(self):
        # OSX handles sizes of dialogs behind our backs, but other platforms need
        # to know about the device pixel ratio
        if sys.platform == "darwin":
            return 1.0
        else:
            # determine a device pixel ratio from font metrics, using the same logic as UM.Theme
            fontPixelRatio = QFontMetrics(QCoreApplication.instance().font()).ascent() / 11
            # round the font pixel ratio to quarters
            fontPixelRatio = int(fontPixelRatio * 4)/4
            return fontPixelRatio

    def _getDefaultLanguage(self, file_name):
        # If we have a language override set in the environment, try and use that.
        lang = os.getenv("URANIUM_LANGUAGE")
        if lang:
            try:
                return Resources.getPath(Resources.i18n, lang, "LC_MESSAGES", file_name + ".qm")
            except FileNotFoundError:
                pass

        # Else, try and get the current language from preferences
        lang = self.getPreferences().getValue("general/language")
        if lang:
            try:
                return Resources.getPath(Resources.i18n, lang, "LC_MESSAGES", file_name + ".qm")
            except FileNotFoundError:
                pass

        # If none of those are set, try to use the environment's LANGUAGE variable.
        lang = os.getenv("LANGUAGE")
        if lang:
            try:
                return Resources.getPath(Resources.i18n, lang, "LC_MESSAGES", file_name + ".qm")
            except FileNotFoundError:
                pass

        # If looking up the language from the enviroment or preferences fails, try and use Qt's system locale instead.
        locale = QLocale.system()

        # First, try and find a directory for any of the provided languages
        for lang in locale.uiLanguages():
            try:
                return Resources.getPath(Resources.i18n, lang, "LC_MESSAGES", file_name + ".qm")
            except FileNotFoundError:
                pass

        # If that fails, see if we can extract a language code from the
        # preferred language, regardless of the country code. This will turn
        # "en-GB" into "en" for example.
        lang = locale.uiLanguages()[0]
        lang = lang[0:lang.find("-")]
        for subdirectory in os.path.listdir(Resources.getPath(Resources.i18n)):
            if subdirectory == "en_7S": #Never automatically go to Pirate.
                continue
            if not os.path.isdir(Resources.getPath(Resources.i18n, subdirectory)):
                continue
            if subdirectory.startswith(lang + "_"): #Only match the language code, not the country code.
                return Resources.getPath(Resources.i18n, lang, "LC_MESSAGES", file_name + ".qm")

        return None


##  Internal.
#
#   Wrapper around a FunctionEvent object to make Qt handle the event properly.
class _QtFunctionEvent(QEvent):
    QtFunctionEvent = QEvent.User + 1

    def __init__(self, fevent):
        super().__init__(self.QtFunctionEvent)
        self._function_event = fevent

