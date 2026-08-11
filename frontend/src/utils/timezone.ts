/**
 * Timezone utility module.
 *
 * Core principles:
 * - Database stores UTC timestamps / UTC time strings (never local time)
 * - Backend processes all business logic in UTC
 * - Frontend is responsible for timezone conversion (UTC → user local time for display)
 * - User timezone is configurable (auto-detect + manual selection)
 */

import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc'
import timezone from 'dayjs/plugin/timezone'
import relativeTime from 'dayjs/plugin/relativeTime'

dayjs.extend(utc)
dayjs.extend(timezone)
dayjs.extend(relativeTime)

const TIMEZONE_STORAGE_KEY = 'user_timezone'
const TIMEZONE_MODE_KEY = 'timezone_mode' // 'auto' | 'manual'

/**
 * Get the user's configured timezone.
 * Priority: manual selection → auto-detected from browser.
 */
export function getUserTimezone(): string {
  const mode = localStorage.getItem(TIMEZONE_MODE_KEY) || 'auto'

  if (mode === 'manual') {
    const stored = localStorage.getItem(TIMEZONE_STORAGE_KEY)
    if (stored) return stored
  }

  // Auto-detect from browser
  return Intl.DateTimeFormat().resolvedOptions().timeZone
}

/**
 * Get the auto-detected timezone from the browser.
 */
export function getAutoDetectedTimezone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone
}

/**
 * Get the current timezone mode ('auto' | 'manual').
 */
export function getTimezoneMode(): 'auto' | 'manual' {
  return (localStorage.getItem(TIMEZONE_MODE_KEY) as 'auto' | 'manual') || 'auto'
}

/**
 * Set the user's timezone preference.
 */
export function setUserTimezone(tz: string, mode: 'auto' | 'manual' = 'manual') {
  localStorage.setItem(TIMEZONE_STORAGE_KEY, tz)
  localStorage.setItem(TIMEZONE_MODE_KEY, mode)
}

/**
 * Reset to auto-detect mode.
 */
export function resetToAutoTimezone() {
  localStorage.setItem(TIMEZONE_MODE_KEY, 'auto')
  localStorage.removeItem(TIMEZONE_STORAGE_KEY)
}

/**
 * Format a UTC timestamp/string to the user's local time for display.
 * This is the PRIMARY function for displaying time in the UI.
 *
 * @param utcTime - UTC time string (ISO 8601) or Unix timestamp (seconds)
 * @param format - dayjs format string (default: 'YYYY-MM-DD HH:mm:ss')
 * @returns Formatted local time string
 */
export function formatLocalTime(
  utcTime: string | number,
  format: string = 'YYYY-MM-DD HH:mm:ss'
): string {
  const tz = getUserTimezone()
  return dayjs.utc(utcTime).tz(tz).format(format)
}

/**
 * Format a UTC time as a relative time string (e.g., "3 minutes ago").
 */
export function formatRelativeTime(utcTime: string | number): string {
  return dayjs.utc(utcTime).fromNow()
}

/**
 * Convert a local time input to UTC for sending to the backend.
 * Use when the user enters a date/time in their local timezone.
 *
 * @param localTime - Local time string
 * @param format - Expected input format
 * @returns ISO 8601 UTC string for API submission
 */
export function localToUTC(localTime: string, format?: string): string {
  const tz = getUserTimezone()
  if (format) {
    return dayjs.tz(localTime, format, tz).utc().toISOString()
  } else {
    return dayjs.tz(localTime, tz).utc().toISOString()
  }
}

/**
 * Get current time in UTC (for display or comparison).
 */
export function nowUTC(): string {
  return dayjs.utc().toISOString()
}

/**
 * Common timezone list for the manual selection dropdown.
 */
export const COMMON_TIMEZONES = [
  'UTC',
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
  'America/Sao_Paulo',
  'Europe/London',
  'Europe/Paris',
  'Europe/Berlin',
  'Europe/Moscow',
  'Asia/Dubai',
  'Asia/Kolkata',
  'Asia/Bangkok',
  'Asia/Shanghai',
  'Asia/Tokyo',
  'Asia/Seoul',
  'Asia/Singapore',
  'Australia/Sydney',
  'Pacific/Auckland',
] as const