/**
 * Jarvis UI - Electron Main Process
 * 
 * Creates a floating HUD window for Jarvis status display.
 */

const { app, BrowserWindow, ipcMain, screen } = require('electron');
const path = require('path');

let mainWindow;

function createWindow() {
    const { width: screenWidth } = screen.getPrimaryDisplay().workAreaSize;
    
    mainWindow = new BrowserWindow({
        width: 320,
        height: 200,
        x: screenWidth - 340,
        y: 20,
        frame: false,
        transparent: true,
        alwaysOnTop: true,
        resizable: false,
        skipTaskbar: true,
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false
        }
    });

    mainWindow.loadFile(path.join(__dirname, 'index.html'));
    
    // Allow clicking through when not hovering
    mainWindow.setIgnoreMouseEvents(true, { forward: true });
    
    // Dev tools in dev mode
    if (process.argv.includes('--dev')) {
        mainWindow.webContents.openDevTools({ mode: 'detach' });
    }
}

app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') {
        app.quit();
    }
});

app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
        createWindow();
    }
});

// IPC handlers for Python backend communication
ipcMain.on('update-status', (event, status) => {
    if (mainWindow) {
        mainWindow.webContents.send('status-updated', status);
    }
});

ipcMain.on('set-listening', (event, isListening) => {
    if (mainWindow) {
        mainWindow.webContents.send('listening-changed', isListening);
    }
});
