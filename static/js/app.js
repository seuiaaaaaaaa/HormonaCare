document.addEventListener("DOMContentLoaded", () => {
    const themeStorageKey = "hormonacare-theme";
    const themeMeta = document.querySelector("meta[name='theme-color']");
    const themeSelect = document.querySelector("[data-theme-select]");
    const themeStatus = document.querySelector("[data-theme-status]");
    const serverTheme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";

    const readStoredTheme = () => {
        try {
            const storedTheme = window.localStorage.getItem(themeStorageKey);
            return storedTheme === "dark" || storedTheme === "light" ? storedTheme : null;
        } catch (error) {
            return null;
        }
    };

    const applyTheme = (themeName, options = {}) => {
        const isDark = themeName === "dark";
        const { persist = true } = options;
        document.documentElement.dataset.theme = themeName;
        document.documentElement.style.colorScheme = themeName;
        document.body.dataset.theme = themeName;
        document.body.classList.toggle("theme-dark", isDark);

        if (themeMeta) {
            themeMeta.setAttribute("content", isDark ? "#0f1729" : "#ff4f87");
        }

        if (themeStatus) {
            themeStatus.textContent = isDark ? "Dark mode preview is on." : "Light mode preview is on.";
        }

        if (themeSelect) {
            themeSelect.value = isDark ? "1" : "0";
        }

        if (persist) {
            try {
                window.localStorage.setItem(themeStorageKey, themeName);
            } catch (error) {
                // Ignore local storage failures and keep the active theme applied.
            }
        }
    };

    applyTheme(readStoredTheme() || serverTheme);

    if (themeSelect) {
        themeSelect.addEventListener("change", () => {
            applyTheme(themeSelect.value === "1" ? "dark" : "light");
        });
    }

    const readNotificationConfig = () => {
        const configNode = document.getElementById("notification-config");
        if (!configNode) {
            return null;
        }
        try {
            return JSON.parse(configNode.textContent || "{}");
        } catch (error) {
            return null;
        }
    };

    const notificationConfig = readNotificationConfig();

    const notificationCenter = (() => {
        const noopCenter = {
            notify: () => null,
            requestPermission: async () => "default",
        };

        if (!notificationConfig || !notificationConfig.enabled || !("Notification" in window) || !window.isSecureContext) {
            window.HormonaCareNotifications = noopCenter;
            return noopCenter;
        }

        const endpoints = notificationConfig.endpoints || {};
        const pages = notificationConfig.pages || {};
        const medicationSchedules = Array.isArray(notificationConfig.medicationSchedules) ? notificationConfig.medicationSchedules : [];
        const preferences = notificationConfig.preferences || {};
        const iconUrl = notificationConfig.iconUrl || "";
        const badgeUrl = notificationConfig.badgeUrl || iconUrl;
        const permissionStorageKey = notificationConfig.permissionStorageKey || "hormonacare-notification-permission-v1";
        const dedupeStorageKey = notificationConfig.dedupeStorageKey || "hormonacare-notification-dedupe-v1";
        const closeAfterMsDefault = Number(notificationConfig.closeAfterMs) || 6500;
        const persistentNotifications = notificationConfig.persistentNotifications !== false;
        const medicationReminderLeadMs = Number(notificationConfig.medicationReminderLeadMs) || 120000;
        const pollIntervals = notificationConfig.pollIntervals || {};
        const defaultPollIntervals = {
            alertsMs: 30000,
            appointmentsMs: 30000,
            assessmentMs: 60000,
        };
        const activeNotifications = new Map();
        const flashMessages = Array.isArray(notificationConfig.flashMessages) ? notificationConfig.flashMessages : [];
        const normalizedFlashMessages = flashMessages
            .map((entry) => (Array.isArray(entry) ? { category: entry[0], message: entry[1] } : entry))
            .filter((entry) => entry && entry.message);

        let flashNotificationsFlushed = false;
        let alertPollTimer = null;
        let appointmentPollTimer = null;
        let assessmentPollTimer = null;
        let assessmentSignature = null;
        let permissionRequestInFlight = false;
        let pushConfigPromise = null;
        let pushSubscriptionPromise = null;
        let medicationReminderRefreshTimer = null;
        let activeMedicationSchedules = [];
        const medicationReminderTimers = new Map();

        const safeReadJson = (storageKey, fallbackValue) => {
            try {
                const rawValue = window.localStorage.getItem(storageKey);
                return rawValue ? JSON.parse(rawValue) : fallbackValue;
            } catch (error) {
                return fallbackValue;
            }
        };

        const safeWriteJson = (storageKey, value) => {
            try {
                window.localStorage.setItem(storageKey, JSON.stringify(value));
            } catch (error) {
                // Ignore storage failures so notifications still work for the current session.
            }
        };

        const dedupeCache = safeReadJson(dedupeStorageKey, {});
        const permissionState = safeReadJson(permissionStorageKey, {
            prompted: false,
            promptedAt: 0,
            lastPermission: Notification.permission,
            enabledNoticeShown: false,
        });

        const pruneDedupeCache = () => {
            const cutoffTime = Date.now() - 7 * 24 * 60 * 60 * 1000;
            Object.keys(dedupeCache).forEach((key) => {
                if (typeof dedupeCache[key] !== "number" || dedupeCache[key] < cutoffTime) {
                    delete dedupeCache[key];
                }
            });
            safeWriteJson(dedupeStorageKey, dedupeCache);
        };

        pruneDedupeCache();

        if (permissionState.lastPermission && permissionState.lastPermission !== Notification.permission) {
            if (
                Notification.permission === "default" &&
                (permissionState.lastPermission === "granted" || permissionState.lastPermission === "denied")
            ) {
                permissionState.prompted = false;
                permissionState.promptedAt = 0;
                permissionState.enabledNoticeShown = false;
            }
            permissionState.lastPermission = Notification.permission;
            safeWriteJson(permissionStorageKey, permissionState);
        } else if (!permissionState.lastPermission) {
            permissionState.lastPermission = Notification.permission;
            safeWriteJson(permissionStorageKey, permissionState);
        }

        if (Notification.permission === "default") {
            permissionState.prompted = false;
            permissionState.promptedAt = 0;
            safeWriteJson(permissionStorageKey, permissionState);
        }

        const notificationsEnabled = () => preferences.general !== false;
        const medicationNotificationsEnabled = () => notificationsEnabled();
        const appointmentNotificationsEnabled = () => notificationsEnabled();
        const alertNotificationsEnabled = () => notificationsEnabled();
        const canNotify = () => notificationsEnabled() && Notification.permission === "granted";

        const sanitizeKey = (value) =>
            String(value || "")
                .toLowerCase()
                .replace(/[^a-z0-9]+/g, "-")
                .replace(/(^-|-$)/g, "")
                .slice(0, 80) || "default";

        const rememberNotification = (dedupeKey) => {
            dedupeCache[dedupeKey] = Date.now();
            safeWriteJson(dedupeStorageKey, dedupeCache);
        };

        const hasRecentNotification = (dedupeKey, ttlMs) => {
            if (activeNotifications.has(dedupeKey)) {
                return true;
            }
            const lastSeen = dedupeCache[dedupeKey];
            return typeof lastSeen === "number" && Date.now() - lastSeen < ttlMs;
        };

        const localDateKey = (dateValue = new Date()) => {
            const year = dateValue.getFullYear();
            const month = String(dateValue.getMonth() + 1).padStart(2, "0");
            const day = String(dateValue.getDate()).padStart(2, "0");
            return `${year}-${month}-${day}`;
        };

        const parseClockTime = (timeString) => {
            const match = /^(\d{2}):(\d{2})/.exec(String(timeString || ""));
            if (!match) {
                return null;
            }
            return {
                hours: Number(match[1]),
                minutes: Number(match[2]),
            };
        };

        const formatClockTime = (timeString) => {
            const parsed = parseClockTime(timeString);
            if (!parsed) {
                return timeString || "";
            }
            const dateValue = new Date();
            dateValue.setHours(parsed.hours, parsed.minutes, 0, 0);
            return dateValue.toLocaleTimeString([], {
                hour: "numeric",
                minute: "2-digit",
            });
        };

        const normalizeMedicationSchedules = (items) =>
            (Array.isArray(items) ? items : [])
                .map((medication) => ({
                    id: Number(medication && medication.id ? medication.id : 0),
                    name: medication && medication.name ? medication.name : "Medication",
                    dosage: medication && medication.dosage ? medication.dosage : "",
                    time_of_day: medication && medication.time_of_day ? medication.time_of_day : "",
                    status: medication && medication.status ? medication.status : "pending",
                    reminder_enabled: !!(medication && medication.reminder_enabled),
                }))
                .filter((medication) => medication.id > 0 && medication.time_of_day);

        const buildMedicationReminderKey = (medication, dateKey) =>
            `medication-${medication.id}-${dateKey}-${String(medication.time_of_day || "").slice(0, 5)}`;

        const clearMedicationReminderTimers = () => {
            medicationReminderTimers.forEach((timerId) => window.clearTimeout(timerId));
            medicationReminderTimers.clear();
            if (medicationReminderRefreshTimer) {
                window.clearTimeout(medicationReminderRefreshTimer);
                medicationReminderRefreshTimer = null;
            }
        };

        const triggerMedicationReminder = (medication, medicationAt) => {
            const now = new Date();
            if (now.getTime() >= medicationAt.getTime()) {
                return null;
            }

            return showNotification(
                "medication_reminder",
                {
                    name: medication.name,
                    dosage: medication.dosage,
                    url: pages.medications,
                    tag: buildMedicationReminderKey(medication, localDateKey(medicationAt)),
                },
                {
                    dedupeKey: buildMedicationReminderKey(medication, localDateKey(medicationAt)),
                    ttlMs: 30 * 60 * 60 * 1000,
                    autoClose: false,
                }
            );
        };

        const scheduleMedicationReminders = (schedules = activeMedicationSchedules) => {
            activeMedicationSchedules = normalizeMedicationSchedules(schedules);
            clearMedicationReminderTimers();

            if (!canNotify() || !medicationNotificationsEnabled()) {
                return;
            }

            const now = new Date();

            activeMedicationSchedules.forEach((medication) => {
                const timeParts = parseClockTime(medication.time_of_day);
                const isPending = medication.status === "pending";

                if (!medication.reminder_enabled || !isPending || !timeParts) {
                    return;
                }

                const medicationAt = new Date(now);
                medicationAt.setHours(timeParts.hours, timeParts.minutes, 0, 0);

                if (medicationAt.getTime() <= now.getTime()) {
                    return;
                }

                const reminderAt = new Date(medicationAt.getTime() - medicationReminderLeadMs);
                const triggerReminder = () => triggerMedicationReminder(medication, medicationAt);

                if (reminderAt.getTime() <= now.getTime()) {
                    triggerReminder();
                    return;
                }

                const timerId = window.setTimeout(triggerReminder, reminderAt.getTime() - now.getTime());
                medicationReminderTimers.set(buildMedicationReminderKey(medication, localDateKey(medicationAt)), timerId);
            });

            const nextRefreshAt = new Date(now);
            nextRefreshAt.setHours(24, 0, 5, 0);
            medicationReminderRefreshTimer = window.setTimeout(() => {
                scheduleMedicationReminders(activeMedicationSchedules);
            }, Math.max(nextRefreshAt.getTime() - now.getTime(), 1000));
        };

        const buildAssessmentSignature = (payload) =>
            JSON.stringify({
                score: Math.round(Number(payload && payload.score ? payload.score : 0)),
                topRecommendation:
                    payload && Array.isArray(payload.recommendations) && payload.recommendations.length
                        ? payload.recommendations[0]
                        : "",
                modelSource: payload && payload.model_source ? payload.model_source : "",
            });

        const classifyNotificationPreference = (type) => {
            if (!notificationsEnabled()) {
                return false;
            }
            return true;
        };

        const buildNotificationContent = (type, payload = {}) => {
            const normalizedPayload = payload || {};
            const defaultAlertUrl = pages.alerts || pages.dashboard || window.location.pathname;
            const defaultMedicationUrl = pages.medications || pages.dashboard || window.location.pathname;
            const defaultAppointmentUrl = pages.appointments || pages.dashboard || window.location.pathname;

            if (type === "medication_reminder") {
                return {
                    title: "Medication Reminder",
                    body: normalizedPayload.body || "Time to take your medication in 2 minutes.",
                    tag: normalizedPayload.tag || `medication-${sanitizeKey(normalizedPayload.name || "reminder")}`,
                    url: normalizedPayload.url || defaultMedicationUrl,
                    requireInteraction: true,
                };
            }

            if (type === "cycle_update") {
                return {
                    title: "Cycle Update",
                    body: normalizedPayload.body || normalizedPayload.message || "New cycle insight available.",
                    tag: normalizedPayload.tag || `cycle-${sanitizeKey(normalizedPayload.message || normalizedPayload.body || "update")}`,
                    url: normalizedPayload.url || defaultAlertUrl,
                };
            }

            if (type === "assessment_available") {
                return {
                    title: "Assessment Available",
                    body: normalizedPayload.body || "A fresh wellness assessment is now available.",
                    tag: normalizedPayload.tag || "assessment-available",
                    url: normalizedPayload.url || defaultAlertUrl,
                };
            }

            if (type === "sync_complete") {
                return {
                    title: "Sync Complete",
                    body: normalizedPayload.body || "Offline data has been uploaded successfully.",
                    tag: normalizedPayload.tag || "sync-complete",
                    url: normalizedPayload.url || pages.dashboard || window.location.pathname,
                };
            }

            if (type === "recitation_result") {
                return {
                    title: "Recitation Result",
                    body: normalizedPayload.body || "Your participation has been recorded.",
                    tag: normalizedPayload.tag || "recitation-result",
                    url: normalizedPayload.url || pages.dashboard || window.location.pathname,
                };
            }

            if (type === "scheduled_task") {
                return {
                    title: "Scheduled Task",
                    body: normalizedPayload.body || normalizedPayload.message || "A scheduled task needs your attention.",
                    tag: normalizedPayload.tag || `scheduled-${sanitizeKey(normalizedPayload.message || normalizedPayload.body || "task")}`,
                    url: normalizedPayload.url || defaultAppointmentUrl,
                };
            }

            if (type === "important_update") {
                return {
                    title: "Important Update",
                    body: normalizedPayload.body || normalizedPayload.message || "A new update is available.",
                    tag: normalizedPayload.tag || `update-${sanitizeKey(normalizedPayload.message || normalizedPayload.body || "update")}`,
                    url: normalizedPayload.url || pages.dashboard || window.location.pathname,
                };
            }

            return {
                title: normalizedPayload.title || "Alert",
                body: normalizedPayload.body || normalizedPayload.message || "A new alert needs your attention.",
                tag: normalizedPayload.tag || `alert-${sanitizeKey(normalizedPayload.title || normalizedPayload.message || "alert")}`,
                url: normalizedPayload.url || defaultAlertUrl,
            };
        };

        const buildBrowserNotificationOptions = (type, content, dedupeKey, requireInteraction) => ({
            body: content.body,
            tag: content.tag,
            icon: iconUrl,
            badge: badgeUrl,
            renotify: false,
            requireInteraction,
            data: {
                url: content.url,
                type,
                dedupeKey,
            },
        });

        const handlePageNotificationClick = (notification, targetUrl) => {
            try {
                window.focus();
            } catch (error) {
                // Ignore focus failures on restricted browser contexts.
            }

            if (targetUrl) {
                const nextUrl = new URL(targetUrl, window.location.origin);
                const currentUrl = new URL(window.location.href);
                if (nextUrl.pathname + nextUrl.search !== currentUrl.pathname + currentUrl.search) {
                    window.location.assign(nextUrl.pathname + nextUrl.search + nextUrl.hash);
                }
            }

            notification.close();
        };

        const showPageNotification = (content, notificationOptions, dedupeKey, shouldAutoClose, closeAfterMs) => {
            try {
                const notification = new Notification(content.title, notificationOptions);

                rememberNotification(dedupeKey);
                activeNotifications.set(dedupeKey, notification);

                notification.onclick = () => {
                    handlePageNotificationClick(notification, content.url);
                };

                notification.onclose = () => {
                    activeNotifications.delete(dedupeKey);
                };

                if (shouldAutoClose) {
                    window.setTimeout(() => {
                        if (typeof notification.close === "function") {
                            notification.close();
                        }
                    }, closeAfterMs);
                }

                return notification;
            } catch (error) {
                return null;
            }
        };

        const showServiceWorkerNotification = async (content, notificationOptions, dedupeKey) => {
            if (!("serviceWorker" in navigator)) {
                return null;
            }

            try {
                const registration = await navigator.serviceWorker.getRegistration();
                if (!registration || typeof registration.showNotification !== "function") {
                    return null;
                }
                await registration.showNotification(content.title, notificationOptions);
                rememberNotification(dedupeKey);
                return true;
            } catch (error) {
                return null;
            }
        };

        const showNotification = (type, payload = {}, options = {}) => {
            if (!canNotify() || !classifyNotificationPreference(type, payload)) {
                return null;
            }

            const content = buildNotificationContent(type, payload);
            const dedupeKey = options.dedupeKey || payload.dedupeKey || content.tag;
            const ttlMs = Number(options.ttlMs || payload.ttlMs) || 5 * 60 * 1000;

            if (!options.force && hasRecentNotification(dedupeKey, ttlMs)) {
                return null;
            }

            const requireInteraction =
                persistentNotifications ||
                options.requireInteraction === true ||
                payload.requireInteraction === true ||
                content.requireInteraction === true;
            const shouldAutoClose =
                !persistentNotifications &&
                options.autoClose !== false &&
                payload.autoClose !== false &&
                requireInteraction !== true;
            const closeAfterMs = Number(options.closeAfterMs || payload.closeAfterMs) || closeAfterMsDefault;
            const notificationOptions = buildBrowserNotificationOptions(type, content, dedupeKey, requireInteraction);

            if (options.serviceWorker !== false) {
                return showServiceWorkerNotification(content, notificationOptions, dedupeKey).then((shown) => {
                    if (shown) {
                        return shown;
                    }
                    return showPageNotification(content, notificationOptions, dedupeKey, shouldAutoClose, closeAfterMs);
                });
            }

            return showPageNotification(content, notificationOptions, dedupeKey, shouldAutoClose, closeAfterMs);
        };

        const showPermissionEnabledNotice = () => {
            if (permissionState.enabledNoticeShown) {
                return null;
            }

            const notice = showNotification(
                "important_update",
                {
                    body: "Browser notifications are now enabled for reminders and important updates.",
                    url: pages.dashboard || window.location.pathname,
                    tag: "notifications-enabled",
                },
                {
                    force: true,
                    dedupeKey: "notifications-enabled",
                    ttlMs: 24 * 60 * 60 * 1000,
                }
            );

            const markNoticeShown = () => {
                permissionState.enabledNoticeShown = true;
                safeWriteJson(permissionStorageKey, permissionState);
            };

            if (notice && typeof notice.then === "function") {
                notice.then((shown) => {
                    if (shown) {
                        markNoticeShown();
                    }
                });
            } else if (notice) {
                markNoticeShown();
            }

            return notice;
        };

        const classifyFlashMessage = (entry) => {
            const message = String(entry && entry.message ? entry.message : "").trim();
            const lowerMessage = message.toLowerCase();

            if (!message) {
                return null;
            }

            if (
                lowerMessage.includes("please log in") ||
                lowerMessage.includes("verify your email") ||
                lowerMessage.includes("otp") ||
                lowerMessage.includes("password") ||
                lowerMessage.includes("sign in")
            ) {
                return null;
            }

            if (lowerMessage.includes("sync") || lowerMessage.includes("uploaded") || lowerMessage.includes("offline data")) {
                return {
                    type: "sync_complete",
                    body: "Offline data has been uploaded successfully.",
                };
            }

            if (lowerMessage.includes("assessment") || lowerMessage.includes("quiz")) {
                return {
                    type: "assessment_available",
                    body: message,
                };
            }

            if (lowerMessage.includes("recitation") || lowerMessage.includes("participation")) {
                return {
                    type: "recitation_result",
                    body: message,
                };
            }

            if (
                lowerMessage.includes("cycle") ||
                lowerMessage.includes("period") ||
                lowerMessage.includes("ovulation") ||
                lowerMessage.includes("flow log")
            ) {
                return {
                    type: "cycle_update",
                    body: message,
                };
            }

            if (lowerMessage.includes("appointment")) {
                return {
                    type: "scheduled_task",
                    body: message,
                };
            }

            if (lowerMessage.includes("reminder") || lowerMessage.includes("alert")) {
                return {
                    type: "alert",
                    body: message,
                };
            }

            if (entry.category === "warning" || entry.category === "danger") {
                return {
                    type: "alert",
                    body: message,
                };
            }

            return null;
        };

        const flushFlashNotifications = () => {
            if (flashNotificationsFlushed || !canNotify()) {
                return;
            }
            normalizedFlashMessages.forEach((entry) => {
                const eventPayload = classifyFlashMessage(entry);
                if (!eventPayload) {
                    return;
                }
                showNotification(eventPayload.type, eventPayload, {
                    dedupeKey: `flash-${sanitizeKey(entry.category)}-${sanitizeKey(entry.message)}`,
                    ttlMs: 2 * 60 * 1000,
                });
            });
            flashNotificationsFlushed = true;
        };

        const fetchApiPayload = async (url, fetchOptions = {}) => {
            if (!url) {
                return null;
            }
            const headers = {
                Accept: "application/json",
                ...(fetchOptions.headers || {}),
            };
            const response = await fetch(url, {
                ...fetchOptions,
                headers: {
                    ...headers,
                },
                credentials: "same-origin",
            });
            const payload = await response.json().catch(() => null);
            if (!response.ok || !payload || payload.ok !== true) {
                throw new Error("Notification polling failed.");
            }
            return payload.data;
        };

        const urlBase64ToUint8Array = (base64String) => {
            const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
            const normalizedBase64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
            const rawData = window.atob(normalizedBase64);
            const outputArray = new Uint8Array(rawData.length);
            for (let index = 0; index < rawData.length; index += 1) {
                outputArray[index] = rawData.charCodeAt(index);
            }
            return outputArray;
        };

        const fetchPushConfig = () => {
            if (!pushConfigPromise) {
                pushConfigPromise = fetchApiPayload(endpoints.notificationConfig).catch(() => null);
            }
            return pushConfigPromise;
        };

        const savePushSubscription = async (subscription) => {
            if (!subscription || !endpoints.subscribeNotifications) {
                return false;
            }
            await fetchApiPayload(endpoints.subscribeNotifications, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({ subscription: subscription.toJSON() }),
            });
            return true;
        };

        const ensurePushSubscription = async () => {
            if (pushSubscriptionPromise) {
                return pushSubscriptionPromise;
            }

            pushSubscriptionPromise = (async () => {
                if (!("serviceWorker" in navigator) || !("PushManager" in window) || Notification.permission !== "granted") {
                    return false;
                }

                const pushConfig = await fetchPushConfig();
                if (!pushConfig || !pushConfig.web_push_enabled || !pushConfig.vapid_public_key) {
                    return false;
                }

                const registration = await navigator.serviceWorker.ready;
                let subscription = await registration.pushManager.getSubscription();
                if (!subscription) {
                    subscription = await registration.pushManager.subscribe({
                        userVisibleOnly: true,
                        applicationServerKey: urlBase64ToUint8Array(pushConfig.vapid_public_key),
                    });
                }
                return savePushSubscription(subscription);
            })().catch(() => {
                pushSubscriptionPromise = null;
                return false;
            });

            return pushSubscriptionPromise;
        };

        const sendServerPushTest = async () => {
            const subscribed = await ensurePushSubscription();
            if (!subscribed || !endpoints.testPush) {
                return false;
            }
            await fetchApiPayload(endpoints.testPush, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({}),
            });
            return true;
        };

        const pickAlertCandidate = (payload) => {
            if (!payload || !Array.isArray(payload.active_alerts)) {
                return null;
            }

            const severityRank = {
                danger: 3,
                warning: 2,
                info: 1,
                neutral: 0,
                success: 0,
            };

            const significantAlerts = payload.active_alerts
                .filter((alert) => alert && alert.title && (severityRank[alert.tone] || 0) >= 2)
                .sort((left, right) => (severityRank[right.tone] || 0) - (severityRank[left.tone] || 0));

            if (significantAlerts.length) {
                return significantAlerts[0];
            }

            if (payload.risk_indicator === "high" && payload.active_alerts.length) {
                return payload.active_alerts[0];
            }

            return null;
        };

        const checkAlerts = async () => {
            if (!canNotify() || !alertNotificationsEnabled() || !endpoints.alerts) {
                return;
            }

            try {
                const payload = await fetchApiPayload(endpoints.alerts);
                const candidate = pickAlertCandidate(payload);
                if (!candidate) {
                    return;
                }

                const combinedAlertText = `${candidate.title || ""} ${candidate.description || ""}`.toLowerCase();
                const type =
                    combinedAlertText.includes("cycle") ||
                    combinedAlertText.includes("period") ||
                    combinedAlertText.includes("ovulation") ||
                    combinedAlertText.includes("flow")
                        ? "cycle_update"
                        : "alert";

                showNotification(
                    type,
                    {
                        body: candidate.description || candidate.recommendation || candidate.title,
                        message: candidate.title,
                        url: pages.alerts,
                    },
                    {
                        dedupeKey: `alerts-${sanitizeKey(candidate.title)}-${sanitizeKey(candidate.description || candidate.recommendation || "")}`,
                        ttlMs: 2 * 60 * 60 * 1000,
                    }
                );
            } catch (error) {
                // Keep background polling quiet if the API is temporarily unavailable.
            }
        };

        const checkUpcomingAppointments = async () => {
            if (!canNotify() || !appointmentNotificationsEnabled() || !endpoints.appointments) {
                return;
            }

            try {
                const appointments = await fetchApiPayload(endpoints.appointments);
                const now = new Date();
                const todayKey = localDateKey(now);

                appointments.forEach((appointment) => {
                    const meta = appointment && appointment.notes ? appointment.notes : {};
                    const isScheduled = (meta.status || "scheduled") === "scheduled";
                    const reminderEnabled = !!meta.reminder_enabled;

                    if (!reminderEnabled || !isScheduled || !appointment.appointment_date || !appointment.appointment_time) {
                        return;
                    }

                    const scheduledAt = new Date(`${appointment.appointment_date}T${appointment.appointment_time}`);
                    if (Number.isNaN(scheduledAt.getTime()) || localDateKey(scheduledAt) !== todayKey) {
                        return;
                    }

                    const minutesUntil = Math.round((scheduledAt.getTime() - now.getTime()) / 60000);
                    if (minutesUntil < 0 || minutesUntil > 30) {
                        return;
                    }

                    showNotification(
                        "scheduled_task",
                        {
                            body: `Upcoming appointment with ${appointment.doctor_name || "your doctor"} at ${formatClockTime(appointment.appointment_time)}.`,
                            url: pages.appointments,
                        },
                        {
                            dedupeKey: `appointment-${appointment.id}-${appointment.appointment_date}-${appointment.appointment_time}`,
                            ttlMs: 12 * 60 * 60 * 1000,
                        }
                    );
                });
            } catch (error) {
                // Ignore polling errors and keep browser notifications lightweight.
            }
        };

        const checkAssessmentUpdates = async () => {
            if (!canNotify() || !alertNotificationsEnabled() || !endpoints.assessment) {
                return;
            }

            try {
                const assessment = await fetchApiPayload(endpoints.assessment);
                const nextSignature = buildAssessmentSignature(assessment || {});
                if (!assessmentSignature) {
                    assessmentSignature = nextSignature;
                    return;
                }

                if (assessmentSignature !== nextSignature) {
                    assessmentSignature = nextSignature;
                    showNotification(
                        "assessment_available",
                        {
                            body: "A fresh wellness assessment is now available.",
                            url: pages.alerts,
                        },
                        {
                            dedupeKey: `assessment-${sanitizeKey(nextSignature)}`,
                            ttlMs: 60 * 60 * 1000,
                        }
                    );
                }
            } catch (error) {
                // Keep polling silent for flaky or unavailable API responses.
            }
        };

        const startBackgroundMonitoring = () => {
            if (!canNotify()) {
                return;
            }

            flushFlashNotifications();
            checkAlerts();
            scheduleMedicationReminders(medicationSchedules);
            checkUpcomingAppointments();
            checkAssessmentUpdates();

            if (!alertPollTimer && endpoints.alerts) {
                alertPollTimer = window.setInterval(checkAlerts, Number(pollIntervals.alertsMs) || defaultPollIntervals.alertsMs);
            }

            if (!appointmentPollTimer && endpoints.appointments) {
                appointmentPollTimer = window.setInterval(checkUpcomingAppointments, Number(pollIntervals.appointmentsMs) || defaultPollIntervals.appointmentsMs);
            }

            if (!assessmentPollTimer && endpoints.assessment) {
                assessmentPollTimer = window.setInterval(checkAssessmentUpdates, Number(pollIntervals.assessmentMs) || defaultPollIntervals.assessmentMs);
            }
        };

        const requestPermission = async () => {
            if (!notificationsEnabled()) {
                return Notification.permission;
            }

            if (Notification.permission !== "default") {
                permissionState.lastPermission = Notification.permission;
                safeWriteJson(permissionStorageKey, permissionState);
                if (Notification.permission === "granted") {
                    ensurePushSubscription();
                    startBackgroundMonitoring();
                    showPermissionEnabledNotice();
                }
                return Notification.permission;
            }

            if (permissionRequestInFlight || permissionState.prompted) {
                return Notification.permission;
            }

            permissionRequestInFlight = true;
            permissionState.prompted = true;
            permissionState.promptedAt = Date.now();
            safeWriteJson(permissionStorageKey, permissionState);

            try {
                const result = await Notification.requestPermission();
                permissionState.lastPermission = result;
                permissionState.prompted = result === "default" ? false : true;
                permissionState.promptedAt = result === "default" ? 0 : permissionState.promptedAt;
                safeWriteJson(permissionStorageKey, permissionState);
                if (result === "granted") {
                    await ensurePushSubscription();
                    startBackgroundMonitoring();
                    showPermissionEnabledNotice();
                }
                return result;
            } catch (error) {
                permissionState.prompted = false;
                permissionState.promptedAt = 0;
                safeWriteJson(permissionStorageKey, permissionState);
                return Notification.permission;
            } finally {
                permissionRequestInFlight = false;
            }
        };

        const schedulePermissionRequest = () => {
            if (!notificationsEnabled() || Notification.permission !== "default" || permissionState.prompted) {
                return;
            }

            const interactionEvents = ["click", "keydown", "touchstart"];

            const clearInteractionListeners = () => {
                interactionEvents.forEach((eventName) => {
                    document.removeEventListener(eventName, handleInteraction);
                });
            };

            const handleInteraction = () => {
                clearInteractionListeners();
                requestPermission();
            };

            interactionEvents.forEach((eventName) => {
                document.addEventListener(eventName, handleInteraction, { once: true, passive: true });
            });
        };

        window.addEventListener("hormonacare:notify", (event) => {
            const detail = event.detail || {};
            if (!detail.type) {
                return;
            }
            showNotification(detail.type, detail, detail.options || {});
        });

        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "visible" && Notification.permission === "granted") {
                scheduleMedicationReminders(activeMedicationSchedules.length ? activeMedicationSchedules : medicationSchedules);
            }
        });

        const publicApi = {
            notify: (type, payload = {}, options = {}) => showNotification(type, payload, options),
            requestPermission,
            subscribeForPush: ensurePushSubscription,
            testPush: sendServerPushTest,
            rescheduleMedicationReminders: (schedules = medicationSchedules) => scheduleMedicationReminders(schedules),
            test: () =>
                showNotification(
                    "important_update",
                    {
                        body: "This is a test browser notification from HormonaCare.",
                        url: pages.dashboard || window.location.pathname,
                        tag: `notification-test-${Date.now()}`,
                    },
                    {
                        force: true,
                        dedupeKey: `notification-test-${Date.now()}`,
                        ttlMs: 1000,
                    }
                ),
            getStatus: () => ({
                supported: "Notification" in window,
                secureContext: window.isSecureContext,
                permission: Notification.permission,
                preferences: {
                    general: preferences.general !== false,
                },
                prompted: !!permissionState.prompted,
            }),
        };

        window.HormonaCareNotifications = publicApi;

        if (Notification.permission === "granted") {
            ensurePushSubscription();
            startBackgroundMonitoring();
            showPermissionEnabledNotice();
        } else {
            schedulePermissionRequest();
        }

        return publicApi;
    })();

    const notificationTestButton = document.querySelector("[data-notification-test]");
    const notificationStatus = document.querySelector("[data-notification-status]");

    const setNotificationStatus = (message, tone = "") => {
        if (!notificationStatus) {
            return;
        }
        notificationStatus.textContent = message;
        notificationStatus.classList.toggle("is-success", tone === "success");
        notificationStatus.classList.toggle("is-error", tone === "error");
    };

    if (notificationTestButton) {
        notificationTestButton.addEventListener("click", async () => {
            if (!window.HormonaCareNotifications || typeof window.HormonaCareNotifications.getStatus !== "function") {
                setNotificationStatus("Notifications are not available in this browser.", "error");
                return;
            }

            const status = window.HormonaCareNotifications.getStatus();
            if (!status.supported) {
                setNotificationStatus("Notifications are not supported in this browser.", "error");
                return;
            }
            if (!status.secureContext) {
                setNotificationStatus("Use localhost or HTTPS to enable notifications.", "error");
                return;
            }

            notificationTestButton.disabled = true;
            setNotificationStatus("Preparing notification...");

            try {
                const permission = await window.HormonaCareNotifications.requestPermission();
                if (permission !== "granted") {
                    setNotificationStatus("Allow notifications in the browser prompt first.", "error");
                    return;
                }

                const localShown =
                    typeof window.HormonaCareNotifications.test === "function"
                        ? await window.HormonaCareNotifications.test()
                        : false;
                setNotificationStatus(localShown ? "Notification shown. Checking server push..." : "Checking server push...");

                const pushSent =
                    typeof window.HormonaCareNotifications.testPush === "function"
                        ? await window.HormonaCareNotifications.testPush()
                        : false;
                if (pushSent) {
                    setNotificationStatus("Server push notification sent.", "success");
                } else if (localShown) {
                    setNotificationStatus("Browser notification shown. Server push is not ready yet.", "success");
                } else {
                    setNotificationStatus("Server push is not ready yet.", "error");
                }
            } catch (error) {
                setNotificationStatus("Server push is not ready yet.", "error");
            } finally {
                notificationTestButton.disabled = false;
            }
        });
    }

    const mobileMenuOverlay = document.getElementById("mobile-menu-overlay");
    const mobileMenuToggle = document.querySelector(".mobile-menu-toggle");
    const mobileMenuClose = document.querySelector(".mobile-menu-close");

    const closeMobileMenu = () => {
        if (mobileMenuOverlay) {
            mobileMenuOverlay.hidden = true;
        }
        if (mobileMenuToggle) {
            mobileMenuToggle.setAttribute("aria-expanded", "false");
        }
        document.body.classList.remove("menu-open");
    };

    const openMobileMenu = () => {
        if (mobileMenuOverlay) {
            mobileMenuOverlay.hidden = false;
        }
        if (mobileMenuToggle) {
            mobileMenuToggle.setAttribute("aria-expanded", "true");
        }
        document.body.classList.add("menu-open");
    };

    if (mobileMenuToggle) {
        mobileMenuToggle.addEventListener("click", () => {
            if (mobileMenuOverlay && !mobileMenuOverlay.hidden) {
                closeMobileMenu();
            } else {
                openMobileMenu();
            }
        });
    }

    if (mobileMenuClose) {
        mobileMenuClose.addEventListener("click", closeMobileMenu);
    }

    if (mobileMenuOverlay) {
        mobileMenuOverlay.addEventListener("click", (event) => {
            if (event.target === mobileMenuOverlay) {
                closeMobileMenu();
            }
        });
    }

    const formatDateLabel = (isoDate) => {
        const dateValue = new Date(`${isoDate}T00:00:00`);
        return dateValue.toLocaleDateString("en-US", {
            month: "short",
            day: "numeric",
            year: "numeric",
        });
    };

    document.querySelectorAll("input[type='date']").forEach((input) => {
        if (!input.value) {
            input.value = new Date().toISOString().split("T")[0];
        }
    });

    document.querySelectorAll("[data-password-toggle]").forEach((button) => {
        const wrap = button.closest(".auth-input-wrap");
        const input = wrap ? wrap.querySelector("[data-password-field]") : null;
        if (!input) return;

        const syncPasswordToggle = () => {
            const visible = input.type === "text";
            button.setAttribute("aria-pressed", visible ? "true" : "false");
            button.setAttribute("aria-label", visible ? "Hide password" : "Show password");
        };

        button.addEventListener("click", () => {
            input.type = input.type === "password" ? "text" : "password";
            syncPasswordToggle();
        });

        syncPasswordToggle();
    });

    const openModal = (id) => {
        const modal = document.getElementById(id);
        if (modal) {
            modal.hidden = false;
        }
    };

    const closeModal = (id) => {
        const modal = document.getElementById(id);
        if (modal) {
            modal.hidden = true;
        }
    };

    const confirmActionModal = document.getElementById("confirm-action-modal");
    const confirmActionTitle = document.getElementById("confirm-action-title");
    const confirmActionMessage = document.getElementById("confirm-action-message");
    const confirmActionSubmit = document.querySelector("[data-confirm-submit]");
    const confirmActionCancelButtons = document.querySelectorAll("[data-confirm-cancel]");
    let pendingConfirmForm = null;
    let pendingConfirmSubmitter = null;

    const resetConfirmActionModal = () => {
        pendingConfirmForm = null;
        pendingConfirmSubmitter = null;
        if (confirmActionTitle) {
            confirmActionTitle.textContent = "Confirm action";
        }
        if (confirmActionMessage) {
            confirmActionMessage.textContent = "Are you sure you want to continue?";
        }
        if (confirmActionSubmit) {
            confirmActionSubmit.textContent = "Continue";
        }
    };

    const closeConfirmActionModal = () => {
        if (!confirmActionModal) {
            return;
        }
        confirmActionModal.hidden = true;
        resetConfirmActionModal();
    };

    const openConfirmActionModal = (form, submitter = null) => {
        if (!confirmActionModal || !form) {
            return;
        }

        pendingConfirmForm = form;
        pendingConfirmSubmitter = submitter;

        if (confirmActionTitle) {
            confirmActionTitle.textContent = form.dataset.confirmTitle || "Confirm action";
        }
        if (confirmActionMessage) {
            confirmActionMessage.textContent = form.dataset.confirmMessage || "Are you sure you want to continue?";
        }
        if (confirmActionSubmit) {
            confirmActionSubmit.textContent = form.dataset.confirmAction || "Continue";
        }

        confirmActionModal.hidden = false;
        if (confirmActionSubmit) {
            window.setTimeout(() => confirmActionSubmit.focus(), 0);
        }
    };

    document.querySelectorAll("form[data-confirm-dialog]").forEach((form) => {
        form.addEventListener("submit", (event) => {
            if (form.dataset.confirmed === "true") {
                delete form.dataset.confirmed;
                return;
            }

            event.preventDefault();
            openConfirmActionModal(form, event.submitter || null);
        });
    });

    confirmActionCancelButtons.forEach((button) => {
        button.addEventListener("click", (event) => {
            event.preventDefault();
            closeConfirmActionModal();
        });
    });

    if (confirmActionModal) {
        confirmActionModal.addEventListener("click", (event) => {
            if (event.target === confirmActionModal) {
                closeConfirmActionModal();
            }
        });
    }

    if (confirmActionSubmit) {
        confirmActionSubmit.addEventListener("click", () => {
            if (!pendingConfirmForm) {
                closeConfirmActionModal();
                return;
            }

            const form = pendingConfirmForm;
            const submitter = pendingConfirmSubmitter;
            closeConfirmActionModal();
            form.dataset.confirmed = "true";

            try {
                if (submitter && typeof form.requestSubmit === "function") {
                    form.requestSubmit(submitter);
                    return;
                }

                if (typeof form.requestSubmit === "function") {
                    form.requestSubmit();
                    return;
                }
            } catch (error) {
                // Fall back to direct submission if the browser rejects the stored submitter.
            }

            form.submit();
        });
    }

    document.querySelectorAll("[data-modal-open]").forEach((button) => {
        button.addEventListener("click", (event) => {
            event.preventDefault();
            const modalId = button.getAttribute("data-modal-open");
            const logDateInput = document.getElementById("cycle-log-date");
            const modalDateLabel = document.getElementById("modal-date-label");
            if (logDateInput && button.dataset.calendarDate) {
                logDateInput.value = button.dataset.calendarDate;
                if (modalDateLabel) {
                    modalDateLabel.textContent = formatDateLabel(button.dataset.calendarDate);
                }
            }
            openModal(modalId);
        });
    });

    document.querySelectorAll("[data-modal-close]").forEach((button) => {
        button.addEventListener("click", (event) => {
            event.preventDefault();
            closeModal(button.getAttribute("data-modal-close"));
        });
    });

    document.querySelectorAll(".modal-backdrop").forEach((modal) => {
        modal.addEventListener("click", (event) => {
            if (event.target === modal) {
                modal.hidden = true;
            }
        });
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            closeMobileMenu();
            closeConfirmActionModal();
            document.querySelectorAll(".modal-backdrop").forEach((modal) => {
                modal.hidden = true;
            });
        }
    });

    const logDateInput = document.getElementById("cycle-log-date");
    const modalDateLabel = document.getElementById("modal-date-label");

    document.querySelectorAll("[data-calendar-date]").forEach((cell) => {
        cell.addEventListener("click", () => {
            if (logDateInput) {
                logDateInput.value = cell.dataset.calendarDate;
            }
            if (modalDateLabel) {
                modalDateLabel.textContent = formatDateLabel(cell.dataset.calendarDate);
            }
        });
    });

    const symptomsField = document.getElementById("symptoms-field");
    const toggleSymptoms = () => {
        const selected = Array.from(document.querySelectorAll(".symptom-pill.active")).map((pill) => pill.dataset.symptom);
        if (symptomsField) {
            symptomsField.value = selected.join(", ");
        }
    };

    document.querySelectorAll(".symptom-pill").forEach((pill) => {
        if (symptomsField && symptomsField.value.includes(pill.dataset.symptom)) {
            pill.classList.add("active");
        }
        pill.addEventListener("click", () => {
            pill.classList.toggle("active");
            toggleSymptoms();
        });
    });

    const registerForm = document.getElementById("register-form");
    if (registerForm) {
        const fullNameInput = registerForm.querySelector("input[name='full_name']");
        const emailInput = registerForm.querySelector("input[name='email']");
        const passwordInput = document.getElementById("register-password");
        const confirmInput = document.getElementById("register-confirm-password");
        const fullNameError = registerForm.querySelector("[data-full-name-error]");
        const emailError = registerForm.querySelector("[data-email-error]");
        const passwordError = registerForm.querySelector("[data-password-error]");
        const confirmError = registerForm.querySelector("[data-confirm-error]");

        if (registerForm.dataset.clearAutofill === "true") {
            const clearRegisterAutofill = () => {
                if (fullNameInput) fullNameInput.value = "";
                if (emailInput) emailInput.value = "";
                if (passwordInput) passwordInput.value = "";
                if (confirmInput) confirmInput.value = "";
            };

            window.setTimeout(clearRegisterAutofill, 80);
            window.addEventListener("pageshow", clearRegisterAutofill);
        }

        const setFieldState = (input, errorNode, isValid, message) => {
            if (!input || !errorNode) return;
            const wrap = input.closest(".auth-input-wrap");
            if (!wrap) return;
            wrap.classList.toggle("is-invalid", !isValid);
            errorNode.textContent = message;
            errorNode.classList.toggle("hidden-error", isValid);
        };

        const validateFullName = () => {
            const value = fullNameInput ? fullNameInput.value.trim() : "";
            const valid = value.length >= 2 && value.length <= 120 && (value.match(/[A-Za-z]/g) || []).length >= 2;
            setFieldState(fullNameInput, fullNameError, valid || !value, "Enter your full name using 2 to 120 characters.");
            return valid;
        };

        const validateEmail = () => {
            const value = emailInput ? emailInput.value.trim() : "";
            const valid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value) && value.length <= 80;
            setFieldState(emailInput, emailError, valid || !value, "Enter a valid email address.");
            return valid;
        };

        const validatePassword = () => {
            const value = passwordInput ? passwordInput.value : "";
            const valid =
                value.length >= 6 &&
                /[A-Za-z]/.test(value) &&
                /\d/.test(value) &&
                /[^A-Za-z0-9]/.test(value);
            setFieldState(
                passwordInput,
                passwordError,
                valid || !value,
                "Password must be 6+ characters and include 1 letter, 1 number, and 1 special character."
            );
            return valid;
        };

        const validateConfirm = () => {
            const valid = confirmInput && passwordInput && confirmInput.value === passwordInput.value && confirmInput.value.length > 0;
            setFieldState(confirmInput, confirmError, valid || !(confirmInput && confirmInput.value), "Passwords must match.");
            return valid;
        };

        if (fullNameInput) fullNameInput.addEventListener("input", validateFullName);
        if (emailInput) emailInput.addEventListener("input", validateEmail);
        if (passwordInput) passwordInput.addEventListener("input", () => {
            validatePassword();
            validateConfirm();
        });
        if (confirmInput) confirmInput.addEventListener("input", validateConfirm);

        registerForm.addEventListener("submit", (event) => {
            const isFullNameValid = validateFullName();
            const isEmailValid = validateEmail();
            const isPasswordValid = validatePassword();
            const isConfirmValid = validateConfirm();
            if (!isFullNameValid || !isEmailValid || !isPasswordValid || !isConfirmValid) {
                event.preventDefault();
            }
        });
    }

    const resetFlow = document.querySelector("[data-password-reset-flow]");
    if (resetFlow) {
        const requestUrl = resetFlow.dataset.requestUrl;
        const verifyUrl = resetFlow.dataset.verifyUrl;
        const updateUrl = resetFlow.dataset.updateUrl;
        const loginUrl = resetFlow.dataset.loginUrl;
        const subtitle = document.querySelector("[data-reset-subtitle]");
        const statusNode = resetFlow.querySelector("[data-reset-status]");
        const hiddenIdentifierInput = resetFlow.querySelector("[data-reset-identifier-value]");
        const identifierInput = resetFlow.querySelector("[data-reset-identifier-input]");
        const identifierDisplays = resetFlow.querySelectorAll("[data-reset-identifier-display]");
        const identifierError = resetFlow.querySelector("[data-reset-identifier-error]");
        const otpInput = resetFlow.querySelector("[data-reset-otp-input]");
        const otpError = resetFlow.querySelector("[data-reset-otp-error]");
        const passwordInput = resetFlow.querySelector("[data-reset-password-input]");
        const confirmInput = resetFlow.querySelector("[data-reset-confirm-input]");
        const passwordError = resetFlow.querySelector("[data-reset-password-error]");
        const confirmError = resetFlow.querySelector("[data-reset-confirm-error]");
        const panels = Array.from(resetFlow.querySelectorAll("[data-reset-step-panel]"));
        const chips = Array.from(resetFlow.querySelectorAll("[data-reset-step-chip]"));
        const resendButton = resetFlow.querySelector("[data-reset-resend-button]");
        const backButton = resetFlow.querySelector("[data-reset-back-button]");
        let currentStep = "identify";
        let activeRequestCount = 0;

        const subtitleByStep = {
            identify: "Enter your email or username and we'll send a reset code.",
            otp: "Enter the 6-digit code sent to your email.",
            password: "Choose a new password for your account.",
        };

        const setFieldState = (input, errorNode, isValid, message) => {
            if (!input || !errorNode) return;
            const wrap = input.closest(".auth-input-wrap");
            if (wrap) {
                wrap.classList.toggle("is-invalid", !isValid);
            }
            errorNode.textContent = message || "";
            errorNode.classList.toggle("hidden-error", isValid);
        };

        const clearFieldState = (input, errorNode) => setFieldState(input, errorNode, true, "");

        const setBusy = (isBusy) => {
            activeRequestCount = isBusy ? activeRequestCount + 1 : Math.max(0, activeRequestCount - 1);
            const disabled = activeRequestCount > 0;
            resetFlow.querySelectorAll("button").forEach((button) => {
                button.disabled = disabled;
            });
        };

        const showStatus = (message, tone = "info") => {
            if (!statusNode) return;
            statusNode.textContent = message;
            statusNode.classList.remove("hidden-error", "is-error", "is-success", "is-info");
            statusNode.classList.add(`is-${tone}`);
        };

        const hideStatus = () => {
            if (!statusNode) return;
            statusNode.textContent = "";
            statusNode.classList.add("hidden-error");
            statusNode.classList.remove("is-error", "is-success", "is-info");
        };

        const syncIdentifier = (value) => {
            const cleanValue = (value || "").trim().toLowerCase();
            if (hiddenIdentifierInput) {
                hiddenIdentifierInput.value = cleanValue;
            }
            if (identifierInput) {
                identifierInput.value = cleanValue;
            }
            identifierDisplays.forEach((node) => {
                node.textContent = cleanValue;
            });
            return cleanValue;
        };

        const setStep = (stepName) => {
            currentStep = stepName;
            panels.forEach((panel) => {
                panel.classList.toggle("is-active", panel.dataset.resetStepPanel === stepName);
            });
            chips.forEach((chip) => {
                chip.classList.toggle("is-active", chip.dataset.resetStepChip === stepName);
            });
            if (subtitle) {
                subtitle.textContent = subtitleByStep[stepName] || subtitleByStep.identify;
            }

            if (stepName === "identify" && identifierInput) {
                identifierInput.focus();
            } else if (stepName === "otp" && otpInput) {
                otpInput.focus();
            } else if (stepName === "password" && passwordInput) {
                passwordInput.focus();
            }
        };

        const parseResponse = async (response) => {
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw data;
            }
            return data;
        };

        const validatePassword = () => {
            const value = passwordInput ? passwordInput.value : "";
            const valid =
                value.length >= 6 &&
                /[A-Za-z]/.test(value) &&
                /\d/.test(value) &&
                /[^A-Za-z0-9]/.test(value);
            setFieldState(
                passwordInput,
                passwordError,
                valid || !value,
                "Password must be 6+ characters and include 1 letter, 1 number, and 1 special character."
            );
            return valid;
        };

        const validateConfirm = () => {
            const valid = confirmInput && passwordInput && confirmInput.value === passwordInput.value && confirmInput.value.length > 0;
            setFieldState(confirmInput, confirmError, valid || !(confirmInput && confirmInput.value), "Passwords do not match.");
            return valid;
        };

        const requestOtp = async () => {
            hideStatus();
            clearFieldState(identifierInput, identifierError);
            const identifier = syncIdentifier(identifierInput ? identifierInput.value : "");
            if (!identifier) {
                setFieldState(identifierInput, identifierError, false, "Enter your email or username.");
                return;
            }

            setBusy(true);
            try {
                const response = await fetch(requestUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ identifier }),
                });
                const data = await parseResponse(response);
                syncIdentifier(data.identifier || identifier);
                if (otpInput) otpInput.value = "";
                clearFieldState(otpInput, otpError);
                setStep("otp");
                showStatus(data.message || "A 6-digit code has been sent to your email.", "success");
            } catch (error) {
                setFieldState(identifierInput, identifierError, false, error.message || "We could not send a reset code right now.");
            } finally {
                setBusy(false);
            }
        };

        const verifyOtp = async () => {
            hideStatus();
            clearFieldState(otpInput, otpError);
            const identifier = syncIdentifier(hiddenIdentifierInput ? hiddenIdentifierInput.value : "");
            const otp = (otpInput ? otpInput.value : "").trim();
            if (!/^\d{6}$/.test(otp)) {
                setFieldState(otpInput, otpError, false, "Enter the 6-digit code.");
                return;
            }

            setBusy(true);
            try {
                const response = await fetch(verifyUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ identifier, otp }),
                });
                await parseResponse(response);
                clearFieldState(passwordInput, passwordError);
                clearFieldState(confirmInput, confirmError);
                if (passwordInput) passwordInput.value = "";
                if (confirmInput) confirmInput.value = "";
                setStep("password");
                showStatus("Code verified. You can now create a new password.", "success");
            } catch (error) {
                if (error.field === "identifier") {
                    setStep("identify");
                    setFieldState(identifierInput, identifierError, false, error.message || "This account is not registered. Please sign up.");
                } else {
                    setFieldState(otpInput, otpError, false, error.message || "Invalid or expired code.");
                }
            } finally {
                setBusy(false);
            }
        };

        const updatePassword = async () => {
            hideStatus();
            const identifier = syncIdentifier(hiddenIdentifierInput ? hiddenIdentifierInput.value : "");
            const isPasswordValid = validatePassword();
            const isConfirmValid = validateConfirm();
            if (!isPasswordValid || !isConfirmValid) {
                return;
            }

            setBusy(true);
            try {
                const response = await fetch(updateUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        identifier,
                        password: passwordInput ? passwordInput.value : "",
                        confirm_password: confirmInput ? confirmInput.value : "",
                    }),
                });
                const data = await parseResponse(response);
                showStatus(data.message || "Password updated successfully.", "success");
                window.setTimeout(() => {
                    window.location.href = data.redirect_url || loginUrl || "/login";
                }, 1200);
            } catch (error) {
                const targetField = error.field;
                const message = error.message || "We could not update your password right now.";
                if (targetField === "identifier") {
                    setStep("identify");
                    setFieldState(identifierInput, identifierError, false, message);
                } else if (targetField === "otp") {
                    setStep("otp");
                    setFieldState(otpInput, otpError, false, message);
                } else if (targetField === "confirm_password") {
                    setFieldState(confirmInput, confirmError, false, message);
                } else {
                    setFieldState(passwordInput, passwordError, false, message);
                }
            } finally {
                setBusy(false);
            }
        };

        if (passwordInput) {
            passwordInput.addEventListener("input", () => {
                validatePassword();
                validateConfirm();
            });
        }

        if (confirmInput) {
            confirmInput.addEventListener("input", validateConfirm);
        }

        if (resendButton) {
            resendButton.addEventListener("click", (event) => {
                event.preventDefault();
                requestOtp();
            });
        }

        if (backButton) {
            backButton.addEventListener("click", (event) => {
                event.preventDefault();
                hideStatus();
                clearFieldState(otpInput, otpError);
                setStep("identify");
            });
        }

        resetFlow.addEventListener("submit", (event) => {
            event.preventDefault();
            if (currentStep === "identify") {
                requestOtp();
            } else if (currentStep === "otp") {
                verifyOtp();
            } else {
                updatePassword();
            }
        });

        syncIdentifier(hiddenIdentifierInput ? hiddenIdentifierInput.value : "");
        setStep("identify");
    }

    const resetForm = document.getElementById("reset-password-form");
    if (resetForm) {
        const passwordInput = document.getElementById("reset-password");
        const confirmInput = document.getElementById("reset-confirm-password");
        const passwordError = resetForm.querySelector("[data-reset-password-error]");
        const confirmError = resetForm.querySelector("[data-reset-confirm-error]");

        const setFieldState = (input, errorNode, isValid, message) => {
            const wrap = input.closest(".auth-input-wrap");
            if (!wrap || !errorNode) return;
            wrap.classList.toggle("is-invalid", !isValid);
            errorNode.textContent = message;
            errorNode.classList.toggle("hidden-error", isValid);
        };

        const validatePassword = () => {
            const value = passwordInput.value;
            const valid =
                value.length >= 6 &&
                /[A-Za-z]/.test(value) &&
                /\d/.test(value) &&
                /[^A-Za-z0-9]/.test(value);
            setFieldState(
                passwordInput,
                passwordError,
                valid || !value,
                "Password must be 6+ characters and include 1 letter, 1 number, and 1 special character."
            );
            return valid;
        };

        const validateConfirm = () => {
            const valid = confirmInput.value === passwordInput.value && confirmInput.value.length > 0;
            setFieldState(confirmInput, confirmError, valid || !confirmInput.value, "Passwords must match.");
            return valid;
        };

        passwordInput.addEventListener("input", () => {
            validatePassword();
            validateConfirm();
        });
        confirmInput.addEventListener("input", validateConfirm);

        resetForm.addEventListener("submit", (event) => {
            const isPasswordValid = validatePassword();
            const isConfirmValid = validateConfirm();
            if (!isPasswordValid || !isConfirmValid) {
                event.preventDefault();
            }
        });
    }

    const loginForm = document.querySelector("[data-login-form]");
    if (loginForm && loginForm.dataset.clearAutofill === "true") {
        const emailInput = loginForm.querySelector("input[name='email']");
        const passwordInput = loginForm.querySelector("input[name='password']");

        const clearLoginAutofill = () => {
            if (emailInput) {
                emailInput.value = "";
            }
            if (passwordInput) {
                passwordInput.value = "";
            }
        };

        window.setTimeout(clearLoginAutofill, 80);
        window.addEventListener("pageshow", clearLoginAutofill);
    }

    const settingsPasswordModal = document.querySelector("[data-settings-password-modal]");
    if (settingsPasswordModal) {
        const verifyCurrentUrl = settingsPasswordModal.dataset.verifyCurrentUrl;
        const sendOtpUrl = settingsPasswordModal.dataset.sendOtpUrl;
        const verifyOtpUrl = settingsPasswordModal.dataset.verifyOtpUrl;
        const updatePasswordUrl = settingsPasswordModal.dataset.updatePasswordUrl;
        const flow = settingsPasswordModal.querySelector("[data-settings-password-flow]");
        const statusNode = settingsPasswordModal.querySelector("[data-settings-password-status]");
        const emailDisplays = settingsPasswordModal.querySelectorAll("[data-settings-password-email-display]");
        const panels = Array.from(settingsPasswordModal.querySelectorAll("[data-settings-password-panel]"));
        const methodButtons = Array.from(settingsPasswordModal.querySelectorAll("[data-settings-password-method]"));
        const openButtons = Array.from(document.querySelectorAll("[data-settings-password-open]"));
        const backButtons = Array.from(settingsPasswordModal.querySelectorAll("[data-settings-password-back]"));
        const resendButton = settingsPasswordModal.querySelector("[data-settings-resend-otp]");
        const closeButtons = Array.from(settingsPasswordModal.querySelectorAll("[data-modal-close='settings-password-modal']"));
        const userEmail = settingsPasswordModal.dataset.userEmail || "";
        const currentPasswordInput = settingsPasswordModal.querySelector("[data-settings-current-password]");
        const currentPasswordError = settingsPasswordModal.querySelector("[data-settings-current-password-error]");
        const otpInput = settingsPasswordModal.querySelector("[data-settings-otp-input]");
        const otpError = settingsPasswordModal.querySelector("[data-settings-otp-error]");
        const newPasswordInput = settingsPasswordModal.querySelector("[data-settings-new-password]");
        const newPasswordError = settingsPasswordModal.querySelector("[data-settings-new-password-error]");
        const confirmPasswordInput = settingsPasswordModal.querySelector("[data-settings-confirm-password]");
        const confirmPasswordError = settingsPasswordModal.querySelector("[data-settings-confirm-password-error]");
        let currentStep = "method";
        let activeRequestCount = 0;
        let closeTimer = null;

        const setFieldState = (input, errorNode, isValid, message) => {
            if (!input || !errorNode) return;
            const wrap = input.closest(".auth-input-wrap");
            if (wrap) {
                wrap.classList.toggle("is-invalid", !isValid);
            }
            errorNode.textContent = message || "";
            errorNode.classList.toggle("hidden-error", isValid);
        };

        const clearFieldState = (input, errorNode) => setFieldState(input, errorNode, true, "");

        const syncEmailDisplays = () => {
            emailDisplays.forEach((node) => {
                node.textContent = userEmail;
            });
        };

        const showStatus = (message, tone = "info") => {
            if (!statusNode) return;
            statusNode.textContent = message;
            statusNode.classList.remove("hidden-error", "is-error", "is-success", "is-info");
            statusNode.classList.add(`is-${tone}`);
        };

        const hideStatus = () => {
            if (!statusNode) return;
            statusNode.textContent = "";
            statusNode.classList.add("hidden-error");
            statusNode.classList.remove("is-error", "is-success", "is-info");
        };

        const setBusy = (isBusy) => {
            activeRequestCount = isBusy ? activeRequestCount + 1 : Math.max(0, activeRequestCount - 1);
            const disabled = activeRequestCount > 0;
            flow.querySelectorAll("button, input").forEach((node) => {
                node.disabled = disabled;
            });
        };

        const resetPasswordVisibility = () => {
            settingsPasswordModal.querySelectorAll("[data-password-toggle]").forEach((button) => {
                const wrap = button.closest(".auth-input-wrap");
                const input = wrap ? wrap.querySelector("[data-password-field]") : null;
                if (!input) return;
                input.type = "password";
                button.setAttribute("aria-pressed", "false");
                button.setAttribute("aria-label", "Show password");
            });
        };

        const setStep = (stepName) => {
            currentStep = stepName;
            panels.forEach((panel) => {
                panel.classList.toggle("is-active", panel.dataset.settingsPasswordPanel === stepName);
            });

            if (stepName === "method" && methodButtons[0]) {
                methodButtons[0].focus();
            } else if (stepName === "current" && currentPasswordInput) {
                currentPasswordInput.focus();
            } else if (stepName === "otp" && otpInput) {
                otpInput.focus();
            } else if (stepName === "new" && newPasswordInput) {
                newPasswordInput.focus();
            }
        };

        const clearSensitiveInputs = () => {
            [currentPasswordInput, otpInput, newPasswordInput, confirmPasswordInput].forEach((input) => {
                if (input) {
                    input.value = "";
                }
            });
        };

        const clearErrors = () => {
            clearFieldState(currentPasswordInput, currentPasswordError);
            clearFieldState(otpInput, otpError);
            clearFieldState(newPasswordInput, newPasswordError);
            clearFieldState(confirmPasswordInput, confirmPasswordError);
        };

        const resetFlow = () => {
            if (closeTimer) {
                window.clearTimeout(closeTimer);
                closeTimer = null;
            }
            clearSensitiveInputs();
            clearErrors();
            hideStatus();
            resetPasswordVisibility();
            setStep("method");
        };

        const parseResponse = async (response) => {
            const contentType = response.headers.get("content-type") || "";
            const data = contentType.includes("application/json") ? await response.json().catch(() => ({})) : {};
            if (!contentType.includes("application/json")) {
                throw { message: "Your session expired. Please refresh and sign in again." };
            }
            if (!response.ok) {
                throw data;
            }
            return data;
        };

        const validatePassword = () => {
            const value = newPasswordInput ? newPasswordInput.value : "";
            const valid =
                value.length >= 6 &&
                /[A-Za-z]/.test(value) &&
                /\d/.test(value) &&
                /[^A-Za-z0-9]/.test(value);
            setFieldState(
                newPasswordInput,
                newPasswordError,
                valid || !value,
                "Password must be 6+ characters and include 1 letter, 1 number, and 1 special character."
            );
            return valid;
        };

        const validateConfirm = () => {
            const valid =
                confirmPasswordInput &&
                newPasswordInput &&
                confirmPasswordInput.value === newPasswordInput.value &&
                confirmPasswordInput.value.length > 0;
            setFieldState(confirmPasswordInput, confirmPasswordError, valid || !(confirmPasswordInput && confirmPasswordInput.value), "Passwords do not match.");
            return valid;
        };

        const verifyCurrentPassword = async () => {
            hideStatus();
            clearFieldState(currentPasswordInput, currentPasswordError);
            const currentPassword = currentPasswordInput ? currentPasswordInput.value : "";
            if (!currentPassword) {
                setFieldState(currentPasswordInput, currentPasswordError, false, "Enter your current password.");
                return;
            }

            setBusy(true);
            try {
                const response = await fetch(verifyCurrentUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ current_password: currentPassword }),
                });
                const data = await parseResponse(response);
                clearFieldState(newPasswordInput, newPasswordError);
                clearFieldState(confirmPasswordInput, confirmPasswordError);
                if (currentPasswordInput) currentPasswordInput.value = "";
                if (newPasswordInput) newPasswordInput.value = "";
                if (confirmPasswordInput) confirmPasswordInput.value = "";
                setStep("new");
                showStatus(data.message || "Current password verified.", "success");
            } catch (error) {
                setFieldState(currentPasswordInput, currentPasswordError, false, error.message || "Current password is incorrect.");
            } finally {
                setBusy(false);
            }
        };

        const sendOtp = async () => {
            hideStatus();
            clearFieldState(otpInput, otpError);
            setStep("otp");
            showStatus("Sending a 6-digit code to your email...", "info");

            setBusy(true);
            try {
                const response = await fetch(sendOtpUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({}),
                });
                const data = await parseResponse(response);
                if (otpInput) otpInput.value = "";
                showStatus(data.message || "A 6-digit code has been sent to your email.", "success");
            } catch (error) {
                hideStatus();
                setFieldState(otpInput, otpError, false, error.message || "We could not send an OTP right now.");
            } finally {
                setBusy(false);
            }
        };

        const verifyOtp = async () => {
            hideStatus();
            clearFieldState(otpInput, otpError);
            const otp = (otpInput ? otpInput.value : "").trim();
            if (!/^\d{6}$/.test(otp)) {
                setFieldState(otpInput, otpError, false, "Enter the 6-digit code.");
                return;
            }

            setBusy(true);
            try {
                const response = await fetch(verifyOtpUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ otp }),
                });
                const data = await parseResponse(response);
                clearFieldState(newPasswordInput, newPasswordError);
                clearFieldState(confirmPasswordInput, confirmPasswordError);
                if (newPasswordInput) newPasswordInput.value = "";
                if (confirmPasswordInput) confirmPasswordInput.value = "";
                setStep("new");
                showStatus(data.message || "OTP verified.", "success");
            } catch (error) {
                setFieldState(otpInput, otpError, false, error.message || "Invalid or expired code.");
            } finally {
                setBusy(false);
            }
        };

        const updatePassword = async () => {
            hideStatus();
            const isPasswordValid = validatePassword();
            const isConfirmValid = validateConfirm();
            if (!isPasswordValid || !isConfirmValid) {
                return;
            }

            setBusy(true);
            try {
                const response = await fetch(updatePasswordUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        password: newPasswordInput ? newPasswordInput.value : "",
                        confirm_password: confirmPasswordInput ? confirmPasswordInput.value : "",
                    }),
                });
                const data = await parseResponse(response);
                showStatus(data.message || "Password changed successfully.", "success");
                clearSensitiveInputs();
                clearErrors();
                resetPasswordVisibility();
                closeTimer = window.setTimeout(() => {
                    resetFlow();
                    settingsPasswordModal.hidden = true;
                }, 1400);
            } catch (error) {
                if (error.field === "confirm_password") {
                    setFieldState(confirmPasswordInput, confirmPasswordError, false, error.message || "Passwords do not match.");
                } else if (error.field === "otp") {
                    if ((error.message || "").toLowerCase().includes("verify your chosen method first")) {
                        setStep("method");
                        showStatus(error.message || "Verify your chosen method first.", "error");
                    } else {
                        setStep("otp");
                        setFieldState(otpInput, otpError, false, error.message || "Verify your chosen method first.");
                    }
                } else if (error.field === "current_password") {
                    setStep("current");
                    setFieldState(currentPasswordInput, currentPasswordError, false, error.message || "Current password is incorrect.");
                } else {
                    setFieldState(newPasswordInput, newPasswordError, false, error.message || "We could not update your password right now.");
                }
            } finally {
                setBusy(false);
            }
        };

        methodButtons.forEach((button) => {
            button.addEventListener("click", () => {
                clearErrors();
                hideStatus();
                clearSensitiveInputs();
                resetPasswordVisibility();
                if (button.dataset.settingsPasswordMethod === "current") {
                    setStep("current");
                } else {
                    sendOtp();
                }
            });
        });

        backButtons.forEach((button) => {
            button.addEventListener("click", (event) => {
                event.preventDefault();
                clearSensitiveInputs();
                clearErrors();
                hideStatus();
                resetPasswordVisibility();
                setStep("method");
            });
        });

        openButtons.forEach((button) => {
            button.addEventListener("click", () => {
                resetFlow();
                syncEmailDisplays();
            });
        });

        closeButtons.forEach((button) => {
            button.addEventListener("click", () => {
                resetFlow();
            });
        });

        settingsPasswordModal.addEventListener("click", (event) => {
            if (event.target === settingsPasswordModal) {
                window.setTimeout(resetFlow, 0);
            }
        });

        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && !settingsPasswordModal.hidden) {
                window.setTimeout(resetFlow, 0);
            }
        });

        if (otpInput) {
            otpInput.addEventListener("input", () => {
                otpInput.value = otpInput.value.replace(/\D/g, "").slice(0, 6);
            });
        }

        if (newPasswordInput) {
            newPasswordInput.addEventListener("input", () => {
                validatePassword();
                validateConfirm();
            });
        }

        if (confirmPasswordInput) {
            confirmPasswordInput.addEventListener("input", validateConfirm);
        }

        if (resendButton) {
            resendButton.addEventListener("click", (event) => {
                event.preventDefault();
                sendOtp();
            });
        }

        flow.addEventListener("submit", (event) => {
            event.preventDefault();
            if (currentStep === "current") {
                verifyCurrentPassword();
            } else if (currentStep === "otp") {
                verifyOtp();
            } else if (currentStep === "new") {
                updatePassword();
            }
        });

        syncEmailDisplays();
        resetFlow();
    }

    const stressSlider = document.getElementById("stress-slider");
    const stressValue = document.getElementById("stress-value");
    if (stressSlider && stressValue) {
        const syncStressValue = () => {
            stressValue.textContent = `${stressSlider.value} / 5`;
        };
        stressSlider.addEventListener("input", syncStressValue);
        syncStressValue();
    }

    const moodChoices = document.querySelectorAll(".mood-choice");
    if (moodChoices.length) {
        const syncMoodChoices = () => {
            moodChoices.forEach((choice) => {
                const input = choice.querySelector("input[type='radio']");
                choice.classList.toggle("active", !!(input && input.checked));
            });
        };
        moodChoices.forEach((choice) => {
            const input = choice.querySelector("input[type='radio']");
            if (input) {
                input.addEventListener("change", syncMoodChoices);
            }
        });
        syncMoodChoices();
    }
});

if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
        navigator.serviceWorker.register("/static/service-worker.js").catch(() => {
            // Keep registration failure silent for local demo environments.
        });
    });
}
