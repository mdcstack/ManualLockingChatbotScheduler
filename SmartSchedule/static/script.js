// --- Global variables ---
let calendar; // FullCalendar instance
let scheduleData = { schedule: [], tasks: [], tests: [], generated_plan: [], setup_complete: false };

// Store data for the currently clicked event for API actions
let currentEventData = null;

document.addEventListener('DOMContentLoaded', () => {
    // 1. Fetch initial data and then initialize the calendar
    fetchAndInitialize();

    // 2. Chat Input Logic
    const userInput = document.getElementById('user-input');
    if (userInput) {
        userInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
            }
        });
    }

    // 3. Setup Manual Entry Modal
    setupManualEntryListeners();

    // 4. Setup Notification Popup
    document.addEventListener('click', function(event) {
        const popup = document.getElementById('notificationPopup');
        const bellIconContainer = document.querySelector('.notification-icon-container');
        if (popup && popup.style.display === 'block' && !popup.contains(event.target) && bellIconContainer && !bellIconContainer.contains(event.target)) {
            closeNotificationPopup();
        }
    });

    // 5. PDF Generation Listener (Added here inside DOMContentLoaded)
    const pdfBtn = document.getElementById('btn-generate-pdf');
    if (pdfBtn) {
        pdfBtn.addEventListener('click', () => {
            if (!scheduleData.setup_complete) {
                document.getElementById('pdfWarningModal').classList.remove('hidden');
            } else {
                document.getElementById('pdfConfirmModal').classList.remove('hidden');
            }
        });
    }

    // Setup PDF Modal Buttons
    setupPDFModalHandlers();
});

// === CALENDAR INITIALIZATION LOGIC ===

function initializeCalendar() {
    const calendarEl = document.getElementById('calendar');
    calendar = new FullCalendar.Calendar(calendarEl, {
        initialView: 'timeGridWeek',

        // --- CUSTOM BUTTONS (Manual Add) ---
        customButtons: {
            addTaskButton: {
                text: '+ Add Task',
                click: function() {
                    const modal = document.getElementById('manualTaskModal');
                    const dateInput = document.getElementById('manual-task-deadline');

                    const now = new Date();
                    const year = now.getFullYear();
                    const month = String(now.getMonth() + 1).padStart(2, '0');
                    const day = String(now.getDate()).padStart(2, '0');
                    dateInput.min = `${year}-${month}-${day}`;

                    modal.classList.remove('hidden');
                }
            }
        },
        headerToolbar: {
            left: 'prev,next today',
            center: 'title',
            right: 'addTaskButton dayGridMonth,timeGridWeek,timeGridDay'
        },

        slotMinTime: '01:00:00',
        slotMaxTime: '23:00:00',

        allDaySlot: true,
        height: '100%',
        events: fetchCalendarEvents,
        eventClick: function(info) {
            openEventModal(info.event);
        }
    });
    calendar.render();
}

async function fetchAndInitialize() {
    const clientTimestamp = new Date().toISOString();
    const res = await fetch(`/get_schedule?client_timestamp=${clientTimestamp}`);
    const data = await res.json();

    if (data.error) {
        console.error("Error fetching initial schedule:", data.error);
        return;
    }
    scheduleData = data;

    initializeCalendar();

    if (scheduleData.setup_complete) {
        enableDashboardMode();
    }
}

// === CORE DATA FETCHING ===
async function fetchCalendarEvents(fetchInfo, successCallback, failureCallback) {
  try {
    const clientTimestamp = new Date().toISOString();
    const res = await fetch(`/get_schedule?client_timestamp=${clientTimestamp}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    scheduleData = data;

    if (scheduleData.setup_complete) {
        const inputField = document.getElementById('user-input');
        if (inputField && !inputField.disabled) {
            enableDashboardMode();
        }
    }

    let events = [];

    if (data.generated_plan) {
      data.generated_plan.forEach(item => {
        const isDone = item.completed === true;
        events.push({
          title: item.task,
          start: `${item.date}T${item.start_time}:00`,
          end: `${item.date}T${item.end_time}:00`,
          color: isDone ? '#10b981' : '#3788d8',
          extendedProps: { type: 'plan', isDone: isDone }
        });
      });
    }

    if (data.tasks) {
      data.tasks.forEach(item => {
        if (item.deadline) {
           events.push({
             title: `DUE: ${item.name}`,
             start: item.deadline.split('T')[0],
             color: '#e74c3c',
             allDay: true,
             extendedProps: { type: 'task' }
           });
        }
      });
    }

    if (data.tests) {
        data.tests.forEach(item => {
          if (item.date) {
             events.push({
               title: `TEST: ${item.name}`,
               start: item.date,
               color: '#d35400',
               allDay: true,
               extendedProps: { type: 'test' }
             });
          }
        });
    }

    if (data.schedule) {
        const dayMap = { "Sunday": 0, "Monday": 1, "Tuesday": 2, "Wednesday": 3, "Thursday": 4, "Friday": 5, "Saturday": 6 };
        let currentStart = new Date(fetchInfo.start);
        for (let d = 0; d < 7; d++) {
            let loopDate = new Date(currentStart);
            loopDate.setDate(loopDate.getDate() + d);
            let dayNameIndex = loopDate.getDay();

            data.schedule.forEach(cls => {
                if (dayMap[cls.day] === dayNameIndex) {

                    // --- FIX START: Use Local Time Construction ---
                    const year = loopDate.getFullYear();
                    const month = String(loopDate.getMonth() + 1).padStart(2, '0');
                    const day = String(loopDate.getDate()).padStart(2, '0');
                    let dateStr = `${year}-${month}-${day}`;
                    // --- FIX END ---

                    events.push({
                        title: cls.subject,
                        start: `${dateStr}T${cls.start_time}:00`,
                        end: `${dateStr}T${cls.end_time}:00`,
                        color: '#7f8c8d',
                        extendedProps: { type: 'class' }
                    });
                }
            });
        }
    }
    successCallback(events);
    updateNotificationList();
  } catch (e) {
    console.error("Error fetching schedule:", e);
    failureCallback(e);
  }
}

// === SEND MESSAGE ===
async function sendMessage(messageOverride = null) {
  const input = document.getElementById("user-input");
  const chatBox = document.getElementById("chat-box");
  const guide = document.getElementById("chat-guide");
  const userMessage = messageOverride || input.value.trim();

  if (!userMessage || !chatBox || !input) return;

  if (guide) { guide.style.display = 'none'; }

  if (!messageOverride) {
    chatBox.innerHTML += `<div class="message user-message">${userMessage}</div>`;
  } else {
    chatBox.innerHTML += `<div class="message user-message"><em>(Selected priority: ${userMessage.split(": ")[1]})</em></div>`;
  }

  input.value = "";
  setTimeout(() => { chatBox.scrollTop = chatBox.scrollHeight; }, 0);
  showThinkingIndicator();

  const clientTimestamp = new Date().toISOString();
  try {
      const res = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: userMessage,
          year: new Date().getFullYear().toString(),
          client_timestamp: clientTimestamp
        })
      });
      removeThinkingIndicator();
      if (!res.ok) throw new Error(`HTTP error! status: ${res.status}`);
      const data = await res.json();
      handleChatResponse(data);
      if (calendar) calendar.refetchEvents();
  } catch (error) {
       console.error("Error sending message:", error);
       removeThinkingIndicator();
       chatBox.innerHTML += `<div class="message bot-message" style="color: red;">Error: Could not get reply from server.</div>`;
  }
}

// === PDF MODAL HANDLERS ===
function setupPDFModalHandlers() {
    // Modal 1: Warning
    document.getElementById('pdf-warn-cancel').onclick = () => {
        document.getElementById('pdfWarningModal').classList.add('hidden');
    };
    document.getElementById('pdf-warn-continue').onclick = () => {
        document.getElementById('pdfWarningModal').classList.add('hidden');
        document.getElementById('pdfConfirmModal').classList.remove('hidden');
    };

    // Modal 2: Final Confirmation
    document.getElementById('pdf-confirm-no').onclick = () => {
        document.getElementById('pdfConfirmModal').classList.add('hidden');
    };
    document.getElementById('pdf-confirm-yes').onclick = async () => {
        // 1. Trigger Download & Clear
        window.location.href = '/api/generate_pdf_and_clear';

        // 2. UI Reset
        document.getElementById('pdfConfirmModal').classList.add('hidden');
        setTimeout(() => location.reload(), 2000);
    };
}

// === UX/MODAL HELPERS ===
function showThinkingIndicator() {
    const chatBox = document.getElementById("chat-box");
    if (document.getElementById("bot-thinking-indicator")) return;
    const thinkingDiv = document.createElement('div');
    thinkingDiv.id = "bot-thinking-indicator";
    thinkingDiv.className = "message bot-message";
    thinkingDiv.innerHTML = "<em>Thinking...</em>";
    chatBox.appendChild(thinkingDiv);
    chatBox.scrollTop = chatBox.scrollHeight;
}

function removeThinkingIndicator() {
    const indicator = document.getElementById("bot-thinking-indicator");
    if (indicator) { indicator.remove(); }
}

function handleChatResponse(data) {
    const chatBox = document.getElementById("chat-box");
    if (!data || !data.reply) {
        chatBox.innerHTML += `<div class="message bot-message" style="color: red;">Error: Received an invalid response.</div>`;
        return;
    }
    let formattedReply = data.reply.replace(/\*\*(.*?)\*\*/g, '<b>$1</b>');
    chatBox.innerHTML += `<div class="message bot-message">${formattedReply.replace(/\n/g, '<br>')}</div>`;
    setTimeout(() => { chatBox.scrollTop = chatBox.scrollHeight; }, 0);

    // --- NEW LOGIC START: CONFLICT DETECTION TRIGGER ---
    if (data.action === 'show_conflict_modal' && data.modal_data) {
        // Pass the conflict data payload to the new function
        openConflictModal(data.modal_data);
    }
    // --- NEW LOGIC END ---

    if (data.action === 'lock_ui') {
        setTimeout(() => { enableDashboardMode(); }, 3000);
    }
    if (data.action === 'show_priority_modal' && data.options) {
        openPriorityModal(data.options);
    }
}

function openPriorityModal(options) {
    const modal = document.getElementById('priorityConflictModal');
    const content = document.getElementById('priority-modal-body-content');
    const buttons = document.getElementById('priority-modal-footer-buttons');

    if (!modal) return;

    buttons.innerHTML = '';
    content.innerHTML = '<p>The AI planner found two tasks with the same deadline and priority. Which one should it work on first?</p>';

    options.forEach(optionName => {
        const button = document.createElement('button');
        button.className = 'modal-button-primary';
        button.textContent = `Prioritize: ${optionName}`;
        button.addEventListener('click', () => {
            sendMessage(`User priority choice: ${optionName}`);
            modal.classList.add('hidden');
        });
        buttons.appendChild(button);
    });

    const autoButton = document.createElement('button');
    autoButton.className = 'modal-button-secondary';
    autoButton.textContent = 'Decide for Me (Auto)';
    autoButton.addEventListener('click', () => {
        sendMessage('User priority choice: Auto');
        modal.classList.add('hidden');
    });
    buttons.appendChild(autoButton);
    modal.classList.remove('hidden');
}

function toggleNotificationPopup() {
    const popup = document.getElementById('notificationPopup');
    popup.style.display = (popup.style.display === 'block') ? 'none' : 'block';
}
function closeNotificationPopup() { document.getElementById('notificationPopup').style.display = 'none'; }

function updateNotificationList() {
    const listDiv = document.getElementById('notification-list');
    listDiv.innerHTML = '';
    const now = new Date();
    let hasItems = false;

    const sortedTasks = (scheduleData.tasks || [])
        .slice()
        .sort((a, b) => new Date(a.deadline) - new Date(b.deadline));

    if (sortedTasks.length > 0) {
        sortedTasks.forEach(task => {
            if(task.deadline) {
                const d = new Date(task.deadline);
                if (d > now) {
                    hasItems = true;
                    listDiv.innerHTML += `<p><b>${task.name}</b> - Due ${d.toLocaleDateString()} at ${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</p>`;
                }
            }
        });
    }
    if(!hasItems) listDiv.innerHTML = '<p>No pending tasks found.</p>';
}

// === MANUAL ENTRY LOGIC ===

function setupManualEntryListeners() {
    const modal = document.getElementById('manualTaskModal');
    const closeBtn = document.getElementById('manual-task-close');
    const cancelBtn = document.getElementById('manual-task-cancel');
    const saveBtn = document.getElementById('manual-task-save');

    // Helper to close modal
    const closeManualModal = () => {
        modal.classList.add('hidden');
        document.getElementById('manual-task-name').value = '';
        document.getElementById('manual-task-deadline').value = '';
        document.getElementById('manual-task-priority').value = '';
    };

    // Note: The "Open" listener is now inside FullCalendar's customButtons

    if (closeBtn) closeBtn.addEventListener('click', closeManualModal);
    if (cancelBtn) cancelBtn.addEventListener('click', closeManualModal);

    if (saveBtn) {
        saveBtn.addEventListener('click', async () => {
            const name = document.getElementById('manual-task-name').value.trim();
            const type = document.getElementById('manual-task-type').value;
            const rawDate = document.getElementById('manual-task-deadline').value; // YYYY-MM-DD

            // Handle Optional Priority
            let priority = document.getElementById('manual-task-priority').value;
            if (!priority || priority === "") priority = "medium";

            if (!name || !rawDate) {
                alert("Please provide at least a Name and a Date.");
                return;
            }

            // Auto-append 11:59 PM
            const fullDeadline = `${rawDate}T23:59:59`;

            saveBtn.textContent = "Saving...";
            saveBtn.disabled = true;

            try {
                const clientTimestamp = new Date().toISOString();

                const res = await fetch('/api/manual_save_item', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name: name,
                        type: type,
                        deadline: fullDeadline,
                        priority: priority,
                        client_timestamp: clientTimestamp
                    })
                });

                const data = await res.json();

                if (data.error) {
                    alert("Error: " + data.error);
                } else {
                    closeManualModal();
                    if (calendar) calendar.refetchEvents();
                    updateNotificationList();
                }

            } catch (e) {
                console.error("Error saving manual task:", e);
                alert("An error occurred while saving.");
            } finally {
                saveBtn.textContent = "Save Task";
                saveBtn.disabled = false;
            }
        });
    }
}

// === EVENT ACTION LOGIC (Delete / Mark Done) ===

function openEventModal(event) {
    const modal = document.getElementById('eventDetailsModal');
    const titleEl = document.getElementById('event-modal-title');
    const timeEl = document.getElementById('event-modal-time');
    const typeEl = document.getElementById('event-modal-type');
    const btnDone = document.getElementById('btn-mark-done');
    const btnDelete = document.getElementById('btn-delete-event');

    // Store data for API actions
    currentEventData = {
        title: event.title,
        start: event.startStr,
        type: event.extendedProps.type || 'task'
    };

    // UI Updates
    titleEl.textContent = event.title;

    // Time Display Logic
    if (event.allDay) {
        timeEl.textContent = event.start.toLocaleDateString() + " 11:59 PM";
    } else {
        timeEl.textContent = event.start.toLocaleString();
    }

    // --- FIX STARTS HERE ---

    // Check specific types instead of a generic "!= plan"
    if (currentEventData.type === 'class') {
        typeEl.textContent = "Type: Weekly Class";
        btnDone.style.display = 'none';
        btnDelete.textContent = "Delete Class"; // Custom button text for classes
    }
    else if (currentEventData.type === 'task') {
        typeEl.textContent = "Type: Assignment Deadline";
        btnDone.style.display = 'none';
        btnDelete.textContent = "Delete Task & Sessions";
    }
    else if (currentEventData.type === 'test') {
        typeEl.textContent = "Type: Exam / Test Date";
        btnDone.style.display = 'none';
        btnDelete.textContent = "Delete Test & Sessions";
    }
    else {
        // This handles 'plan' (Blue Study Blocks)
        typeEl.textContent = "Type: Study Session";
        btnDelete.textContent = "Delete This Session";

        if (event.extendedProps.isDone) {
            btnDone.style.display = 'none';
        } else {
            btnDone.style.display = 'block';
        }
    }

    modal.classList.remove('hidden');

    // Attach Listeners via onclick (simplest way to prevent stacking listeners)
    btnDelete.onclick = () => handleEventAction('delete');
    btnDone.onclick = () => handleEventAction('done');

    // Close Logic
    document.getElementById('event-modal-close').onclick = () => modal.classList.add('hidden');
}

async function handleEventAction(action) {
    if (!currentEventData) return;

    const endpoint = action === 'delete' ? '/api/delete_event' : '/api/mark_event_done';
    const btn = action === 'delete' ? document.getElementById('btn-delete-event') : document.getElementById('btn-mark-done');

    btn.textContent = "Processing...";
    btn.disabled = true;

    try {
        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(currentEventData)
        });
        const data = await res.json();

        if (data.status === 'success') {
            document.getElementById('eventDetailsModal').classList.add('hidden');
            if (calendar) calendar.refetchEvents();
        } else {
            alert("Error: " + data.error);
        }
    } catch (e) {
        console.error(e);
        alert("Action failed.");
    } finally {
        btn.disabled = false;
        btn.textContent = action === 'delete' ? "Delete" : "Mark as Done";
    }
}

// === DASHBOARD MODE (UI LOCK - READ ONLY) ===

function enableDashboardMode() {
    console.log("Switching to Dashboard Mode (Input Locked)...");

    // 1. Locate Input Elements
    const inputField = document.getElementById('user-input');
    const sendButton = document.querySelector('.input-area button');

    // 2. Disable the Text Input
    if (inputField) {
        inputField.disabled = true;
        inputField.value = ""; // Clear any text
        inputField.placeholder = "Setup complete. Use manual controls.";

        // Visual cues for disabled state
        inputField.style.backgroundColor = "#f3f4f6";
        inputField.style.cursor = "not-allowed";
    }

    // 3. Disable the Send Button
    if (sendButton) {
        sendButton.disabled = true;
        sendButton.onclick = null; // Remove click handler

        // Visual cues for disabled state
        sendButton.style.backgroundColor = "#9ca3af"; // Gray
        sendButton.style.cursor = "not-allowed";
        sendButton.style.transform = "none"; // Stop hover effects
    }

    // 4. (Optional) Scroll to bottom of chat one last time
    const chatBox = document.getElementById("chat-box");
    if (chatBox) {
        chatBox.scrollTop = chatBox.scrollHeight;
    }
}

// ==========================================================
// === NEW MODULE: CONFLICT DETECTION MODAL (TRAFFIC LIGHTS) ===
// ==========================================================

// Global variable to hold the pending request data from the AI
let pendingConflictData = null;

// --- 1. OPEN THE MODAL (Called when AI returns "show_conflict_modal") ---
function openConflictModal(data) {
    pendingConflictData = data; // Store the original request (Item Name, Deadline, etc.)
    const modal = document.getElementById('conflictModal');

    // 1. Pre-fill with AI's proposed time
    document.getElementById('conflict-start-time').value = data.proposed_data.start_time;
    document.getElementById('conflict-end-time').value = data.proposed_data.end_time;

    // 2. Generate Checkboxes & Run Initial Validation
    renderTrafficLightCheckboxes();
    validateTrafficLights(); // Checks the pre-filled time immediately

    modal.classList.remove('hidden');
}

// --- 2. RENDER CHECKBOXES (One for each day of week) ---
function renderTrafficLightCheckboxes() {
    const container = document.getElementById('conflict-days-container');
    container.innerHTML = '';
    const days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

    days.forEach(day => {
        const wrapper = document.createElement('div');
        wrapper.style.display = "flex";
        wrapper.style.flexDirection = "column";
        wrapper.style.alignItems = "center";
        wrapper.style.fontSize = "0.75rem";

        // The Checkbox
        const cb = document.createElement('input');
        cb.type = "checkbox";
        cb.value = day;
        cb.id = `cb-conflict-${day}`;

        // If AI originally requested this day, check it by default
        if (pendingConflictData.proposed_data.days.includes(day)) {
            cb.checked = true;
        }

        // The Label (e.g., "Mon")
        const label = document.createElement('span');
        label.innerText = day.substring(0, 3);

        wrapper.appendChild(cb);
        wrapper.appendChild(label);
        container.appendChild(wrapper);
    });

    // Add Listeners to Time Inputs to trigger re-validation
    document.getElementById('conflict-start-time').oninput = validateTrafficLights;
    document.getElementById('conflict-end-time').oninput = validateTrafficLights;
}

// --- 3. TRAFFIC LIGHT VALIDATION LOGIC (The Core "Red/Green" Check) ---
function validateTrafficLights() {
    const startTimeStr = document.getElementById('conflict-start-time').value;
    const endTimeStr = document.getElementById('conflict-end-time').value;

    if (!startTimeStr || !endTimeStr) return;

    // Convert input times to minutes for easier comparison
    const startMin = timeToMin(startTimeStr);
    const endMin = timeToMin(endTimeStr);

    // Loop through each checkbox (Sunday -> Saturday)
    const days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

    days.forEach(day => {
        const cb = document.getElementById(`cb-conflict-${day}`);
        const wrapper = cb.parentElement;

        // CHECK 1: Is this day actually free in the user's schedule?
        const isFree = checkDayFree(day, startMin, endMin);

        if (isFree) {
            // GREEN LIGHT
            wrapper.style.color = "#10b981"; // Green text
            cb.disabled = false;
            cb.style.accentColor = "#10b981";
        } else {
            // RED LIGHT
            wrapper.style.color = "#ef4444"; // Red text
            cb.disabled = true;
            cb.checked = false; // Force uncheck
            cb.style.accentColor = "#ef4444";
        }
    });
}

// Helper: Check if a specific Weekday has ANY overlap between Now and Deadline
function checkDayFree(dayName, startMin, endMin) {
    // 1. Check Fixed Classes (Weekly)
    const classConflict = scheduleData.schedule.some(cls => {
        if (cls.day !== dayName) return false;
        const clsStart = timeToMin(cls.start_time);
        const clsEnd = timeToMin(cls.end_time);
        return isOverlap(startMin, endMin, clsStart, clsEnd);
    });
    if (classConflict) return false;

    // 2. Check Existing Study Blocks (Specific Dates)
    // We only check blocks that are NOT the one we are currently trying to schedule
    const planConflict = scheduleData.generated_plan.some(block => {
        // Convert block date (YYYY-MM-DD) to Day Name
        const blockDate = new Date(block.date);
        const blockDayName = blockDate.toLocaleDateString('en-US', { weekday: 'long' });

        if (blockDayName !== dayName) return false;

        const blockStart = timeToMin(block.start_time);
        const blockEnd = timeToMin(block.end_time);
        return isOverlap(startMin, endMin, blockStart, blockEnd);
    });

    return !planConflict; // Return TRUE if no conflicts found
}

// Helper: Simple Minute Overlap Check
function isOverlap(s1, e1, s2, e2) {
    return s1 < e2 && s2 < e1;
}

// Helper: "13:30" -> 810 minutes
function timeToMin(t) {
    const [h, m] = t.split(':').map(Number);
    return h * 60 + m;
}

// --- 4. BUTTON HANDLERS ---

// Updated Zero-Token Cancel Logic
document.getElementById('conflict-btn-cancel').onclick = () => {
    document.getElementById('conflictModal').classList.add('hidden');
    // We simply close the modal. We do not send any message to the AI.
    // This effectively "rewinds" the state to before the user asked to schedule.
};

document.getElementById('conflict-btn-save').onclick = async () => {
    // 1. Gather Revised Data
    const newStart = document.getElementById('conflict-start-time').value;
    const newEnd = document.getElementById('conflict-end-time').value;

    const selectedDays = [];
    document.querySelectorAll('#conflict-days-container input:checked').forEach(cb => {
        selectedDays.push(cb.value);
    });

    if (selectedDays.length === 0) {
        alert("Please select at least one day.");
        return;
    }

    // 2. Send "Force Save" Request to Backend
    // We re-use the original item name from pendingConflictData
    const payload = {
        item_name: pendingConflictData.proposed_data.item_name,
        days: selectedDays,
        start_time: newStart,
        end_time: newEnd,
        force_save: true // New Flag for Backend to skip checks
    };

    await submitResolvedSchedule(payload);

    document.getElementById('conflictModal').classList.add('hidden');
};

async function submitResolvedSchedule(payload) {
    const res = await fetch('/api/resolve_conflict', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    const data = await res.json();
    if(data.status === 'success') {
         fetchAndInitialize(); // Refresh Calendar
         // Inform the user via chat (Client-side only message, not sent to AI)
         const chatBox = document.getElementById("chat-box");
         chatBox.innerHTML += `<div class="message bot-message">System: Conflict resolved. Schedule saved.</div>`;
         setTimeout(() => { chatBox.scrollTop = chatBox.scrollHeight; }, 0);
    }
}