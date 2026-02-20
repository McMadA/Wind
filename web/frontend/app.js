document.addEventListener('DOMContentLoaded', () => {
    // Tab Switching
    const tabs = document.querySelectorAll('.tab-btn');
    const forms = document.querySelectorAll('.sync-form');

    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            tabs.forEach(t => t.classList.remove('active'));
            forms.forEach(f => f.classList.remove('active-form'));

            tab.classList.add('active');
            document.getElementById(`${tab.dataset.target}-form`).classList.add('active-form');
        });
    });

    // Drive to Photos Scope Toggle
    const scopeRadios = document.querySelectorAll('input[name="scope"]');
    const folderGroup = document.getElementById('dp-folder-group');

    scopeRadios.forEach(radio => {
        radio.addEventListener('change', (e) => {
            if (e.target.value === 'folder') {
                folderGroup.style.display = 'block';
                document.getElementById('dp-folder').required = true;
            } else {
                folderGroup.style.display = 'none';
                document.getElementById('dp-folder').required = false;
            }
        });
    });

    // Terminal Logging
    const terminalOutput = document.getElementById('terminal-output');
    const clearLogBtn = document.getElementById('clear-log-btn');

    const appendLog = (msg, type = 'normal') => {
        const line = document.createElement('div');
        line.className = `log-line ${type}`;
        line.textContent = msg;
        terminalOutput.appendChild(line);
        terminalOutput.scrollTop = terminalOutput.scrollHeight;
    };

    clearLogBtn.addEventListener('click', () => {
        terminalOutput.innerHTML = '';
        appendLog('Logs cleared.', 'system-msg');
    });

    // Help Modal
    const helpIcons = document.querySelectorAll('.help-folder-id');
    const helpModal = document.getElementById('help-modal');
    const closeHelpBtn = document.getElementById('close-help-btn');

    helpIcons.forEach(icon => {
        icon.addEventListener('click', () => {
            helpModal.classList.remove('hidden');
        });
    });

    closeHelpBtn.addEventListener('click', () => {
        helpModal.classList.add('hidden');
    });

    helpModal.addEventListener('click', (e) => {
        if (e.target === helpModal) {
            helpModal.classList.add('hidden');
        }
    });

    // Common function to handle SSE streaming
    let currentEventSource = null;

    const startStream = (url, body, btn, spinner, btnText, stopBtn) => {
        // Prevent default form submission redirect

        // Reset UI state
        btn.disabled = true;
        spinner.classList.remove('hidden');
        btnText.textContent = 'Running...';
        stopBtn.classList.remove('hidden');
        appendLog(`--- Starting Task at ${new Date().toLocaleTimeString()} ---`, 'system-msg');

        if (currentEventSource) {
            currentEventSource.close();
        }

        const abortController = new AbortController();
        const signal = abortController.signal;

        const onStop = () => {
            abortController.abort();
            appendLog('Sync stopped by user.', 'warning');
        };

        stopBtn.addEventListener('click', onStop);

        // We use fetch to send the POST payload, but the backend returns text/event-stream.
        // The browser's native EventSource doesn't support POST with a payload body. 
        // So we will use fetch and read the stream manually.

        fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
            signal: signal
        }).then(async response => {
            const reader = response.body.getReader();
            const decoder = new TextDecoder('utf-8');
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });

                // Parse SSE lines
                let parts = buffer.split('\n\n');
                buffer = parts.pop(); // Keep the incomplete part

                for (let part of parts) {
                    if (part.startsWith('data: ')) {
                        const jsonStr = part.substring(6);
                        if (!jsonStr) continue;

                        try {
                            const dataObj = JSON.parse(jsonStr);
                            const textChunk = dataObj.text;

                            if (textChunk.includes('[PROCESS_COMPLETE]')) {
                                appendLog(textChunk, 'system-msg');
                                break;
                            }

                            // Process the chunk character by character to handle \r and \n correctly
                            for (let i = 0; i < textChunk.length; i++) {
                                const char = textChunk[i];

                                if (char === '\n') {
                                    // Start a new line
                                    const line = document.createElement('div');
                                    line.className = 'log-line normal';
                                    terminalOutput.appendChild(line);
                                } else if (char === '\r') {
                                    // Carriage return: in a terminal, this moves the cursor to the beginning of the line.
                                    // We'll mimic this by marking the line for clearing upon the NEXT actual character,
                                    // but if it's immediately followed by \n (i.e. \r\n), we do nothing.
                                    if (i + 1 < textChunk.length && textChunk[i + 1] === '\n') {
                                        continue; // It's just a Windows newline, ignore the \r
                                    } else if (terminalOutput.lastChild) {
                                        terminalOutput.lastChild.setAttribute("data-cr-pending", "true");
                                    }
                                } else {
                                    // Append character to the LAST line
                                    if (!terminalOutput.lastChild || terminalOutput.lastChild.classList.contains('system-msg')) {
                                        const line = document.createElement('div');
                                        line.className = 'log-line normal';
                                        terminalOutput.appendChild(line);
                                    }

                                    if (terminalOutput.lastChild.getAttribute("data-cr-pending") === "true") {
                                        terminalOutput.lastChild.textContent = '';
                                        terminalOutput.lastChild.removeAttribute("data-cr-pending");
                                    }

                                    // Basic coloring logic applied on the fly if error is detected
                                    if (terminalOutput.lastChild.textContent.length < 15) {
                                        const lowerText = textChunk.toLowerCase();
                                        if (lowerText.includes('error') || lowerText.includes('traceback') || lowerText.includes('failed')) terminalOutput.lastChild.className = 'log-line error';
                                        else if (lowerText.includes('success') || lowerText.includes('done')) terminalOutput.lastChild.className = 'log-line success';
                                        else if (lowerText.includes('warn') || lowerText.includes('skipping')) terminalOutput.lastChild.className = 'log-line warning';
                                        else if (lowerText.includes('info')) terminalOutput.lastChild.className = 'log-line info';
                                    }

                                    terminalOutput.lastChild.textContent += char;
                                }
                            }

                            // Autoscroll
                            terminalOutput.scrollTop = terminalOutput.scrollHeight;

                        } catch (e) {
                            console.error("Failed to parse SSE JSON:", e, jsonStr);
                        }
                    }
                }
            }

        }).catch(err => {
            if (err.name === 'AbortError') {
                appendLog('Connection aborted.', 'warning');
            } else {
                appendLog(`Connection Error: ${err.message}`, 'error');
            }
        }).finally(() => {
            btn.disabled = false;
            spinner.classList.add('hidden');
            stopBtn.classList.add('hidden');
            stopBtn.removeEventListener('click', onStop);
            btnText.textContent = url.includes('drive') ? 'Start Sync' : 'Start Batch Upload';
            appendLog(`--- Task Finished ---`, 'system-msg');
        });
    };

    // Form Submissions
    document.getElementById('cloud-drive-form').addEventListener('submit', (e) => {
        e.preventDefault();

        const payload = {
            source: document.getElementById('cd-source').value,
            dest: document.getElementById('cd-dest').value,
            source_path: document.getElementById('cd-source-path').value,
            dest_path: document.getElementById('cd-dest-path').value,
            on_duplicate: document.getElementById('cd-on-duplicate').value,
            move: document.getElementById('cd-move').checked,
            dry_run: document.getElementById('cd-dry-run').checked,
            verbose: document.getElementById('cd-verbose').checked
        };

        const btn = document.getElementById('cd-start-btn');
        const spinner = btn.querySelector('.spinner');
        const btnText = btn.querySelector('.btn-text');
        const stopBtn = document.getElementById('cd-stop-btn');

        startStream('/api/sync/drive', payload, btn, spinner, btnText, stopBtn);
    });

    document.getElementById('drive-photos-form').addEventListener('submit', (e) => {
        e.preventDefault();

        const scope = document.querySelector('input[name="scope"]:checked').value;

        const payload = {
            sync_all: scope === 'all',
            folder: scope === 'folder' ? document.getElementById('dp-folder').value : null,
            workers: parseInt(document.getElementById('dp-workers').value, 10),
            dedup_mode: document.getElementById('dp-dedup').value,
            dry_run: document.getElementById('dp-dry-run').checked
        };

        const btn = document.getElementById('dp-start-btn');
        const spinner = btn.querySelector('.spinner');
        const btnText = btn.querySelector('.btn-text');
        const stopBtn = document.getElementById('dp-stop-btn');

        startStream('/api/sync/photos', payload, btn, spinner, btnText, stopBtn);
    });
});
