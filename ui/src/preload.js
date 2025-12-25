/**
 * Jarvis UI - Preload Script
 * 
 * Exposes IPC to renderer process.
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('jarvis', {
    onStatusUpdate: (callback) => {
        ipcRenderer.on('status-updated', (event, status) => callback(status));
    },
    onListeningChange: (callback) => {
        ipcRenderer.on('listening-changed', (event, isListening) => callback(isListening));
    }
});
