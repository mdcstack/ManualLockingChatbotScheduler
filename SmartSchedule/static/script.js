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
                    // Logic to open the manual entry modal
                    const modal = document.getElementById('manualTaskModal');
                    const dateInput = document.getElementById('manual-task-deadline');

                    // Set min date to today
                    const now = new Date();
                    const year = now.getFullYear();
                    const month = String(now.getMonth() + 1).padStart(2, '0');
                    const day = String(now.getDate()).padStart(2, '0');
                    dateInput.min = `${year}-${month}-${day}`;

                    // Open Modal
                    modal.classList.remove('hidden');
                }
            }
        },
        headerToolbar: {
            left: 'prev,next today',
            center: 'title',
            right: 'addTaskButton dayGridMonth,timeGridWeek,timeGridDay'
        },

        // --- HARDCODED TIME RANGE (1am - 11pm) ---
        slotMinTime: '01:00:00',
        slotMaxTime: '23:00:00',
        // -----------------------------------------

        allDaySlot: true,
        height: '100%',
        events: fetchCalendarEvents,
        // Opens the Event Details Modal
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

    // Initialize Calendar (No preferences passed)
    initializeCalendar();

    // --- Check if Setup is Complete (Load Dashboard Mode) ---
    if (scheduleData.setup_complete) {
        enableDashboardMode();
    }
    // -------------------------------------------------------
}


// === CORE DATA FETCHING ===
async function fetchCalendarEvents(fetchInfo, successCallback, failureCallback) {
  try {
    const clientTimestamp = new Date().toISOString();
    const res = await fetch(`/get_schedule?client_timestamp=${clientTimestamp}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    // Update local data
    scheduleData = data;

    // If setup is complete (e.g. user refreshed page), ensure UI matches
    if (scheduleData.setup_complete) {
        const chatContainer = document.querySelector('.chat-container');
        // We check if input is disabled to avoid re-running if already done
        const inputField = document.getElementById('user-input');
        if (inputField && !inputField.disabled) {
            enableDashboardMode();
        }
    }

    let events = [];

    // A. Map Generated Study Plan (Blue Blocks)
    if (data.generated_plan) {
      data.generated_plan.forEach(item => {
        // CHECK IF COMPLETED
        const isDone = item.completed === true;

        events.push({
          title: item.task,
          start: `${item.date}T${item.start_time}:00`,
          end: `${item.date}T${item.end_time}:00`,
          // Green if done, Blue if not
          color: isDone ? '#10b981' : '#3788d8',
          extendedProps: {
              type: 'plan',
              isDone: isDone
          }
        });
      });
    }

    // B. Map Tasks/Tests Deadlines (Red/Orange All-Day Events)
    if (data.tasks) {
      data.tasks.forEach(item => {
        if (item.deadline) {
           events.push({
             title: `DUE: ${item.name}`,
             start: item.deadline.split('T')[0],
             color: '#e74c3c', // Red
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
               color: '#d35400', // Orange
               allDay: true,
               extendedProps: { type: 'test' }
             });
          }
        });
    }

    // C. Map Classes (Gray Recurring)
    if (data.schedule) {
        const dayMap = { "Sunday": 0, "Monday": 1, "Tuesday": 2, "Wednesday": 3, "Thursday": 4, "Friday": 5, "Saturday": 6 };

        let currentStart = new Date(fetchInfo.start);

        for (let d = 0; d < 7; d++) {
            let loopDate = new Date(currentStart);
            loopDate.setDate(loopDate.getDate() + d);
            let dayNameIndex = loopDate.getDay();

            data.schedule.forEach(cls => {
                if (dayMap[cls.day] === dayNameIndex) {
                    let dateStr = loopDate.toISOString().split('T')[0];
                    events.push({
                        title: cls.subject,
                        start: `${dateStr}T${cls.start_time}:00`,
                        end: `${dateStr}T${cls.end_time}:00`,
                        color: '#7f8c8d', // Gray
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

// === SEND MESSAGE (Time Aware) ===
async function sendMessage(messageOverride = null) {
  const input = document.getElementById("user-input");
  const chatBox = document.getElementById("chat-box");
  const userMessage = messageOverride || input.value.trim();

  if (!userMessage || !chatBox || !input) return;

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
    if (indicator) {
        indicator.remove();
    }
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

    // --- NEW: Check for Lock Action ---
    if (data.action === 'lock_ui') {
        setTimeout(() => {
            enableDashboardMode();
        }, 3000); // Wait 3 seconds so user can read the final message
    }
    // ----------------------------------

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

    // Store data for the buttons to use
    currentEventData = {
        title: event.title,
        start: event.startStr, // ISO string
        type: event.extendedProps.type || 'task'
    };

    // UI Updates
    titleEl.textContent = event.title;

    // FIX: If it is an All-Day event (Task/Test), hardcode the display to 11:59 PM
    if (event.allDay) {
        timeEl.textContent = event.start.toLocaleDateString() + " 11:59 PM";
    } else {
        // For Study Blocks (Blue), show the actual start time
        timeEl.textContent = event.start.toLocaleString();
    }

    // Logic: Hide "Mark Done" if it's not a study block or already done
    if (currentEventData.type !== 'plan') {
        typeEl.textContent = "Type: Deadline / Exam Date";
        btnDone.style.display = 'none';
        btnDelete.textContent = "Delete Task & Sessions";
    } else {
        typeEl.textContent = "Type: Study Session";
        btnDelete.textContent = "Delete This Session";

        if (event.extendedProps.isDone) {
            btnDone.style.display = 'none'; // Already done
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