"""Background scheduler for periodic tasks."""

import logging
import os
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

logger = logging.getLogger("joi.api.scheduler")

# Tick intervals (assuming 60s base interval)
_TICKS_CONFIG_SYNC = 10      # Config sync every ~10 minutes
_TICKS_HOURLY = 60           # Maintenance tasks every ~1 hour
_TICKS_DAILY = 1440          # Daily tasks (24 hours)
_TICKS_MEMBERSHIP = 15       # Membership cache check every ~15 minutes


class Scheduler:
    """
    Background scheduler for periodic tasks (wind/impulse, reminders, etc.)

    Runs as a daemon thread inside the API process.
    Only runs when the service is up - no external cron needed.

    Dependencies are injected via set_dependencies() after construction
    to avoid circular imports.
    """

    def __init__(self, interval_seconds: float = 60.0, startup_delay: float = 10.0):
        self._interval = interval_seconds
        self._startup_delay = startup_delay
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        self._last_tick: Optional[float] = None
        self._tick_count = 0
        self._error_count = 0
        self._daily_tasks_pending: bool = False

        # Dependencies (set via set_dependencies)
        self._memory = None
        self._nonce_store = None
        self._config_push_client = None
        self._hmac_rotator = None
        self._membership_cache = None
        self._wind_orchestrator = None
        self._policy_manager = None
        self._consolidator = None
        self._check_fingerprints: Optional[Callable] = None
        self._get_wind_config: Optional[Callable] = None
        self._generate_proactive_message: Optional[Callable] = None
        self._generate_reminder_message: Optional[Callable] = None
        self._send_to_mesh: Optional[Callable] = None
        self._run_auto_ingestion: Optional[Callable] = None
        self._cleanup_send_caches: Optional[Callable] = None
        self._InboundConversation = None
        self._reminder_manager = None
        self._note_manager = None
        self._message_queue = None

    def set_dependencies(
        self,
        memory,
        nonce_store,
        config_push_client,
        hmac_rotator,
        membership_cache,
        wind_orchestrator,
        policy_manager,
        consolidator,
        check_fingerprints: Callable,
        get_wind_config: Callable,
        generate_proactive_message: Callable,
        generate_reminder_message: Callable,
        send_to_mesh: Callable,
        run_auto_ingestion: Callable,
        cleanup_send_caches: Callable,
        InboundConversation,
        reminder_manager,
        note_manager=None,
        message_queue=None,
    ):
        """Set dependencies after construction to avoid circular imports."""
        self._memory = memory
        self._nonce_store = nonce_store
        self._config_push_client = config_push_client
        self._hmac_rotator = hmac_rotator
        self._membership_cache = membership_cache
        self._wind_orchestrator = wind_orchestrator
        self._policy_manager = policy_manager
        self._consolidator = consolidator
        self._check_fingerprints = check_fingerprints
        self._get_wind_config = get_wind_config
        self._cleanup_send_caches = cleanup_send_caches
        self._generate_proactive_message = generate_proactive_message
        self._generate_reminder_message = generate_reminder_message
        self._send_to_mesh = send_to_mesh
        self._run_auto_ingestion = run_auto_ingestion
        self._InboundConversation = InboundConversation
        self._reminder_manager = reminder_manager
        self._note_manager = note_manager
        self._message_queue = message_queue

    def start(self):
        """Start the scheduler thread."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._thread.start()
        logger.info("Scheduler started", extra={
            "interval_seconds": self._interval,
            "startup_delay_seconds": self._startup_delay,
            "action": "scheduler_start"
        })

    def stop(self):
        """Stop the scheduler thread gracefully."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        logger.info("Scheduler stopped", extra={
            "tick_count": self._tick_count,
            "error_count": self._error_count,
            "action": "scheduler_stop"
        })

    def _scheduler_loop(self):
        """Main scheduler loop."""
        # Wait for startup delay to let service stabilize
        if self._startup_delay > 0:
            logger.debug("Scheduler waiting for startup", extra={"delay_seconds": self._startup_delay})
            if self._stop_event.wait(self._startup_delay):
                return  # Stop requested during startup delay

        # Push config to mesh on startup
        self._startup_config_push()

        logger.info("Scheduler active", extra={"first_tick_in_seconds": self._interval})

        while self._running:
            # Wait for interval (or stop signal)
            if self._stop_event.wait(self._interval):
                break  # Stop requested

            if not self._running:
                break

            # Execute tick with error isolation
            try:
                self._tick()
                self._tick_count += 1
                self._last_tick = time.time()
            except Exception as e:
                self._error_count += 1
                logger.error("Scheduler tick error", extra={
                    "error_count": self._error_count,
                    "error": str(e)
                })
                # Continue running - don't let errors kill the scheduler

    def _tick(self):
        """
        Single scheduler tick - runs periodic maintenance tasks:
        - Auto-ingestion of knowledge files
        - Config sync with mesh
        - Tamper detection
        - Nonce cleanup and FTS integrity checks
        - HMAC rotation checks
        - Membership cache refresh
        - Wind proactive messaging
        - Due reminders
        """
        logger.debug("Scheduler tick", extra={"tick_number": self._tick_count + 1})

        # Auto-ingestion check every tick (cheap if no files)
        self._check_ingestion()

        # Config sync check every ~10 minutes
        if self._tick_count % _TICKS_CONFIG_SYNC == 0:
            self._check_config_sync()

        # Tamper detection every tick (SHA256 is cheap)
        self._check_tamper()

        # Low-priority maintenance tasks every ~1 hour
        if self._tick_count % _TICKS_HOURLY == 0:
            self._cleanup_nonces()
            self._cleanup_send_cache()
            self._check_fts_integrity()

        # Mark daily tasks as due at the 24h boundary (unchanged schedule)
        if self._tick_count % _TICKS_DAILY == 0 and self._tick_count > 0:
            self._daily_tasks_pending = True

        # Fire on the first quiet tick after the boundary (may be same tick if already silent)
        if self._daily_tasks_pending and self._is_system_quiet():
            self._daily_tasks_pending = False
            logger.info("Scheduler: running end-of-day procedures", extra={"action": "daily_tasks_start"})
            self._check_hmac_rotation()
            self._purge_old_reminders()
            self._purge_old_messages()
            self._dedup_wind_topics()
            self._rollup_moods()
            self._rollup_feedback_decay()
            logger.info("Scheduler: end-of-day procedures complete", extra={"action": "daily_tasks_done"})

        # Refresh membership cache (only runs if business mode + dm_group_knowledge)
        if self._tick_count % _TICKS_MEMBERSHIP == 0:
            self._refresh_membership()

        # Wind proactive messaging check every tick
        self._check_wind_impulse()

        # Check for due reminders every tick
        self._check_reminders()

        # Check for due note reminders every tick
        self._check_note_reminders()

    def _check_tamper(self):
        """Check for config file tampering. Shuts down service if detected."""
        if not self._check_fingerprints:
            return
        try:
            changed = self._check_fingerprints()
            if changed:
                logger.critical("SECURITY: config files tampered - SHUTTING DOWN", extra={
                    "tampered_count": len(changed),
                    "action": "tamper_detected"
                })
                for path in changed:
                    logger.critical("SECURITY: Tampered file", extra={"path": path})
                # Give logs time to flush, then terminate entire process
                time.sleep(1)
                import os
                os._exit(78)  # EX_CONFIG - must use _exit to kill process, not just thread
        except Exception as e:
            logger.warning("Scheduler: tamper check failed", extra={"error": str(e)})

    def _cleanup_nonces(self):
        """Cleanup expired nonces from the replay protection store."""
        if self._nonce_store:
            try:
                deleted = self._nonce_store.cleanup_expired()
                if deleted > 0:
                    logger.info("Scheduler: cleaned up expired nonces", extra={"count": deleted})
            except Exception as e:
                logger.warning("Scheduler: nonce cleanup failed", extra={"error": str(e)})

    def _cleanup_send_cache(self):
        """Cleanup stale entries from send rate-limiting caches."""
        if self._cleanup_send_caches:
            try:
                self._cleanup_send_caches()
            except Exception as e:
                logger.warning("Scheduler: send cache cleanup failed", extra={"error": str(e)})

    def _check_fts_integrity(self):
        """Periodic check of FTS index integrity."""
        if not self._memory:
            return
        try:
            integrity = self._memory.check_fts_integrity()
            issues = [
                (name, status) for name, status in integrity.items()
                if not status.get("ok", False)
            ]
            if issues:
                for name, status in issues:
                    if "error" in status:
                        logger.warning("FTS index error", extra={
                            "index": name,
                            "error": status["error"]
                        })
                    else:
                        logger.warning("FTS index out of sync", extra={
                            "index": name,
                            "fts_count": status["fts_count"],
                            "main_count": status["main_count"]
                        })
            else:
                logger.debug("Scheduler: FTS integrity check passed")
        except Exception as e:
            logger.warning("Scheduler: FTS integrity check failed", extra={"error": str(e)})

    def _check_ingestion(self):
        """Check for pending knowledge files to ingest."""
        if not self._run_auto_ingestion or not self._memory:
            return
        try:
            self._run_auto_ingestion(self._memory)
        except Exception as e:
            logger.warning("Scheduler: auto-ingestion failed", extra={"error": str(e)})

    def _check_config_sync(self):
        """Check if config needs to be pushed to mesh."""
        if not self._config_push_client:
            return
        try:
            # First check: did local config change?
            if self._config_push_client.needs_push():
                logger.info("Scheduler: local config changed, pushing to mesh", extra={"action": "config_push"})
                success, result = self._config_push_client.push_config()
                if success:
                    logger.info("Scheduler: config push successful", extra={"config_hash": result[:16]})
                else:
                    logger.warning("Scheduler: config push failed", extra={"error": result})
                return  # Done for this tick

            # Second check: does mesh have what we expect?
            in_sync, reason = self._config_push_client.check_mesh_sync()
            if not in_sync:
                if reason == "mesh_unreachable":
                    logger.debug("Scheduler: mesh unreachable, will retry next tick", extra={"reason": reason})
                elif reason == "mesh_empty":
                    logger.info("Scheduler: mesh has no config (restart?), pushing...", extra={"reason": reason})
                    success, result = self._config_push_client.push_config(force=True)
                    if success:
                        logger.info("Scheduler: config push successful", extra={"config_hash": result[:16]})
                    else:
                        logger.warning("Scheduler: config push failed", extra={"error": result})
                elif reason == "mesh_drift":
                    logger.warning("Scheduler: mesh config drift detected, pushing fresh config...", extra={"reason": reason})
                    success, result = self._config_push_client.push_config(force=True)
                    if success:
                        logger.info("Scheduler: config push successful", extra={"config_hash": result[:16]})
                    else:
                        logger.warning("Scheduler: config push failed", extra={"error": result})
        except Exception as e:
            logger.warning("Scheduler: config sync check failed", extra={"error": str(e)})

    def _check_hmac_rotation(self):
        """Check if HMAC rotation is due (weekly)."""
        if not self._hmac_rotator:
            return
        try:
            if self._hmac_rotator.should_rotate():
                logger.info("Scheduler: HMAC rotation due, rotating...", extra={"action": "hmac_rotation"})
                success, result = self._hmac_rotator.rotate(use_grace_period=True)
                if success:
                    logger.info("Scheduler: HMAC rotation successful", extra={"action": "hmac_rotation", "status": "success"})
                else:
                    logger.warning("Scheduler: HMAC rotation failed", extra={"action": "hmac_rotation", "error": result})
        except Exception as e:
            logger.warning("Scheduler: HMAC rotation check failed", extra={"error": str(e)})

    def _purge_old_messages(self):
        """Hard-delete fully-processed messages older than JOI_MESSAGE_RETENTION_DAYS (0=disabled)."""
        raw = int(os.getenv("JOI_MESSAGE_RETENTION_DAYS", "0"))
        retention_days = 0 if raw == 0 else min(90, raw)
        if not retention_days:
            return
        cutoff_ms = int((time.time() - retention_days * 86400) * 1000)
        try:
            deleted = self._memory.delete_processed_messages_before(cutoff_ms)
            if deleted:
                logger.info(
                    "Purged old messages",
                    extra={"count": deleted, "retention_days": retention_days, "action": "purge_old_messages"},
                )
        except Exception as e:
            logger.warning("Scheduler: message purge failed", extra={"error": str(e)})

    def _purge_old_reminders(self):
        """Purge terminal reminders older than JOI_REMINDER_RETENTION_DAYS (default 180, 0=disabled)."""
        if not self._reminder_manager:
            return
        retention = int(os.getenv("JOI_REMINDER_RETENTION_DAYS", "180"))
        try:
            self._reminder_manager.purge_old(retention)
        except Exception as e:
            logger.warning("Scheduler: reminder purge failed", extra={"error": str(e)})

    def _refresh_membership(self):
        """Refresh group membership cache (only if feature is active)."""
        if not self._membership_cache:
            return
        try:
            # membership_cache.refresh() internally checks if it should be active
            if self._membership_cache.refresh():
                logger.debug("Scheduler: membership cache refreshed")
        except Exception as e:
            logger.warning("Scheduler: membership refresh failed", extra={"error": str(e)})

    def _check_wind_impulse(self):
        """Check Wind proactive messaging impulse for all eligible conversations."""
        if not self._wind_orchestrator or not self._get_wind_config:
            return
        try:
            # Update config from policy manager (in case it changed)
            self._wind_orchestrator.update_config(self._get_wind_config())

            # Run Wind tick - returns list of (conv_id, should_send, skip_reason, topic, score,
            #                                    accumulated_impulse, threshold_offset, threshold)
            # Note: tick() evaluates gates at tick time. The scheduler then does slow LLM generation
            # before sending. This means user activity that arrives during generation is not re-checked
            # here — this is intentional (natural timing feel). The silence gate in impulse.py is the
            # authoritative guard; the occasional overlap adds serendipity to Wind's timing.
            results = self._wind_orchestrator.tick()

            # Process any conversations that should receive proactive messages
            for conv_id, should_send, skip_reason, topic, score, accumulated_impulse, threshold_offset, threshold in results:
                if not should_send or not topic:
                    continue

                # Compact context before sending to ensure clean context when user replies
                self._compact_before_wind(conv_id)

                # Generate proactive message (via MessageQueue so user messages get priority)
                try:
                    if self._message_queue:
                        message_text = self._message_queue.enqueue(
                            message_id=f"wind-{conv_id}-{int(time.time())}",
                            handler=lambda msg: self._generate_proactive_message(
                                topic_title=topic.title,
                                topic_content=topic.content,
                                conversation_id=conv_id,
                                topic_type=topic.topic_type,
                                emotional_context=topic.emotional_context,
                            ),
                            is_owner=False,
                            timeout=600.0,
                        )
                    else:
                        message_text = self._generate_proactive_message(
                            topic_title=topic.title,
                            topic_content=topic.content,
                            conversation_id=conv_id,
                            topic_type=topic.topic_type,
                            emotional_context=topic.emotional_context,
                        )
                except Exception as e:
                    logger.warning("Wind: LLM generation failed", extra={"error": str(e), "conversation_id": conv_id})
                    continue

                if not message_text:
                    logger.warning("Wind: failed to generate message", extra={"conversation_id": conv_id})
                    continue

                # Determine conversation type (DMs start with +, groups are base64)
                is_group = not conv_id.startswith("+")
                conv_type = "group" if is_group else "direct"

                # Create conversation object for _send_to_mesh
                conversation = self._InboundConversation(type=conv_type, id=conv_id)

                # For DMs, recipient is the conversation_id (phone number)
                # For groups, we'd need to handle differently (not supported yet)
                if is_group:
                    logger.warning("Wind: group proactive sends not yet supported", extra={"conversation_id": conv_id})
                    continue

                recipient_id = "owner"  # Proactive sends go to allowlisted users
                recipient_transport_id = conv_id

                # Send the message
                success = self._send_to_mesh(
                    recipient_id=recipient_id,
                    recipient_transport_id=recipient_transport_id,
                    conversation=conversation,
                    text=message_text,
                    reply_to=None,
                    is_critical=False,
                )

                if success:
                    # Generate message_id for tracking
                    message_id = str(uuid.uuid4())

                    # Store outbound message in memory first
                    self._memory.store_message(
                        message_id=message_id,
                        direction="outbound",
                        content_type="text",
                        content_text=f"[JOI-WIND] {message_text}",
                        timestamp=int(time.time() * 1000),
                        conversation_id=conv_id,
                    )

                    # Record successful send with message_id for engagement tracking
                    self._wind_orchestrator.record_proactive_sent(
                        conversation_id=conv_id,
                        topic=topic,
                        impulse_score=score,
                        message_text=message_text,
                        message_id=message_id,
                        accumulated_impulse=accumulated_impulse,
                        threshold_offset=threshold_offset,
                        threshold=threshold,
                    )
                else:
                    logger.error("Wind: failed to send", extra={"conversation_id": conv_id})

            # Check for timed-out topics (no response within timeout period)
            self._wind_orchestrator.check_timeout_topics()

        except Exception as e:
            logger.warning("Scheduler: Wind impulse check failed", extra={"error": str(e)})

    def _check_reminders(self):
        """Check for due reminders and send them."""
        if not self._reminder_manager or not self._generate_reminder_message:
            return
        try:
            from reminders import parse_recurrence_interval

            due_reminders = self._reminder_manager.get_due()
        except Exception as e:
            logger.error("Scheduler: failed to fetch due reminders", extra={"error": str(e)}, exc_info=True)
            return

        for reminder in due_reminders:
            try:
                # Determine recurrence context for prompt
                is_recurring = bool(reminder.recurrence)

                # Generate reminder message (via MessageQueue so user messages get priority)
                try:
                    if self._message_queue:
                        message_text = self._message_queue.enqueue(
                            message_id=f"reminder-{reminder.id}-{int(time.time())}",
                            handler=lambda msg: self._generate_reminder_message(
                                title=reminder.title,
                                conversation_id=reminder.conversation_id,
                                is_recurring=is_recurring,
                                snooze_count=reminder.snooze_count,
                            ),
                            is_owner=False,
                            timeout=600.0,
                        )
                    else:
                        message_text = self._generate_reminder_message(
                            title=reminder.title,
                            conversation_id=reminder.conversation_id,
                            is_recurring=is_recurring,
                            snooze_count=reminder.snooze_count,
                        )
                except Exception as e:
                    logger.warning("Reminder: LLM generation failed", extra={"error": str(e), "reminder_id": reminder.id})
                    continue  # Retry on next tick — don't mark fired for transient queue errors

                if not message_text:
                    logger.warning("Reminder: failed to generate message", extra={
                        "reminder_id": reminder.id,
                        "action": "reminder_generate_fail",
                    })
                    # Mark fired to avoid retrying forever
                    self._reminder_manager.mark_fired(reminder.id)
                    continue

                # Only DM sends for now
                is_group = not reminder.conversation_id.startswith("+")
                if is_group:
                    logger.warning("Reminder: group sends not yet supported", extra={
                        "reminder_id": reminder.id,
                    })
                    self._reminder_manager.mark_fired(reminder.id)
                    continue

                conv_type = "direct"
                conversation = self._InboundConversation(type=conv_type, id=reminder.conversation_id)

                success = self._send_to_mesh(
                    recipient_id="owner",
                    recipient_transport_id=reminder.conversation_id,
                    conversation=conversation,
                    text=message_text,
                    reply_to=None,
                    is_critical=False,
                )

                if success:
                    message_id = str(uuid.uuid4())

                    self._memory.store_message(
                        message_id=message_id,
                        direction="outbound",
                        content_type="text",
                        content_text=f"[JOI-REMINDER] {message_text}",
                        timestamp=int(time.time() * 1000),
                        conversation_id=reminder.conversation_id,
                    )

                    # Mark fired then reschedule if recurring
                    self._reminder_manager.mark_fired(reminder.id)
                    if reminder.recurrence:
                        interval = parse_recurrence_interval(reminder.recurrence)
                        if interval and reminder.due_at:
                            new_due = reminder.due_at + interval
                            self._reminder_manager.reschedule(reminder.id, new_due)

                    # If Wind fired recently, push cooldown so user has 30 min to reply
                    if self._wind_orchestrator:
                        state = self._wind_orchestrator.state_manager.get_state(reminder.conversation_id)
                        if state and state.last_proactive_sent_at:
                            elapsed_min = (datetime.now(timezone.utc) - state.last_proactive_sent_at).total_seconds() / 60
                            if elapsed_min < 30:
                                cooldown_min = self._wind_orchestrator.config.min_cooldown_minutes
                                self._wind_orchestrator.state_manager.update_state(
                                    reminder.conversation_id,
                                    last_proactive_sent_at=datetime.now(timezone.utc) - timedelta(minutes=cooldown_min - 30),
                                )

                    logger.info("Reminder sent", extra={
                        "reminder_id": reminder.id,
                        "title": reminder.title[:30],
                        "conversation_id": reminder.conversation_id,
                        "recurring": is_recurring,
                        "action": "reminder_sent",
                    })
                else:
                    logger.error("Reminder: failed to send", extra={
                        "reminder_id": reminder.id,
                        "conversation_id": reminder.conversation_id,
                        "action": "reminder_send_fail",
                    })

            except Exception as e:
                logger.error("Scheduler: reminder processing failed", extra={
                    "reminder_id": reminder.id,
                    "error": str(e),
                }, exc_info=True)

    def _check_note_reminders(self):
        """Check for notes with a past remind_at and send soft notifications."""
        if not self._note_manager:
            return
        try:
            due_notes = self._note_manager.get_due_reminders()
        except Exception as e:
            logger.error("Scheduler: failed to fetch due note reminders", extra={"error": str(e)}, exc_info=True)
            return

        for note in due_notes:
            try:
                # Only DM sends
                is_group = not note.conversation_id.startswith("+")
                if is_group:
                    logger.warning("Note reminder: group sends not yet supported", extra={"note_id": note.id})
                    self._note_manager.clear_remind_at(note.id)
                    continue

                preview = note.content[:100] + ("..." if len(note.content) > 100 else "")
                message_text = f"A note you flagged: **{note.title}**"
                if preview and not self._policy_manager.is_privacy_mode():
                    message_text += f" — {preview}"

                conv_type = "direct"
                conversation = self._InboundConversation(type=conv_type, id=note.conversation_id)

                success = self._send_to_mesh(
                    recipient_id="owner",
                    recipient_transport_id=note.conversation_id,
                    conversation=conversation,
                    text=message_text,
                    reply_to=None,
                    is_critical=False,
                )

                if success:
                    if self._memory:
                        message_id = str(uuid.uuid4())
                        self._memory.store_message(
                            message_id=message_id,
                            direction="outbound",
                            content_type="text",
                            content_text=f"[JOI-NOTE-REMINDER] {message_text}",
                            timestamp=int(time.time() * 1000),
                            conversation_id=note.conversation_id,
                        )
                    self._note_manager.clear_remind_at(note.id)
                    logger.info("Note reminder sent", extra={
                        "note_id": note.id,
                        "conversation_id": note.conversation_id,
                        "action": "note_reminder_sent",
                    })
                else:
                    logger.error("Note reminder: failed to send", extra={
                        "note_id": note.id,
                        "conversation_id": note.conversation_id,
                        "action": "note_reminder_send_fail",
                    })

            except Exception as e:
                logger.error("Scheduler: note reminder processing failed", extra={
                    "note_id": note.id,
                    "error": str(e),
                }, exc_info=True)

    def _compact_before_wind(self, conversation_id: str) -> None:
        """Compact ALL context before Wind send for a fresh start."""
        if not self._consolidator or not self._memory:
            return

        msg_count = self._memory.get_message_count_for_conversation(conversation_id)

        if msg_count <= 0:
            return  # Nothing to compact

        logger.info("Wind: compacting all context before send", extra={
            "conversation_id": conversation_id,
            "message_count": msg_count,
            "action": "wind_compact"
        })

        try:
            self._consolidator._consolidate_conversation(
                conversation_id=conversation_id,
                context_messages=0,  # Not used when compact_all=True
                compact_batch_size=0,  # Not used when compact_all=True
                archive_instead_of_delete=False,
                compact_all=True,
            )
        except Exception as e:
            logger.warning("Wind: compaction failed", extra={
                "conversation_id": conversation_id,
                "error": str(e)
            })

    def _is_system_quiet(self) -> bool:
        """Return True if no allowlisted conversation has been recently active."""
        if not self._wind_orchestrator:
            return True
        config = self._wind_orchestrator.config
        if not config.allowlist:
            return True
        threshold_s = config.daily_tasks_silence_minutes * 60
        now = datetime.now(timezone.utc)
        for conv_id in config.allowlist:
            state = self._wind_orchestrator.state_manager.get_state(conv_id)
            if state and state.last_user_interaction_at:
                elapsed = (now - state.last_user_interaction_at).total_seconds()
                if elapsed < threshold_s:
                    logger.debug("Daily tasks deferred: conversation recently active", extra={
                        "conversation_id": conv_id,
                        "elapsed_minutes": round(elapsed / 60, 1),
                        "required_minutes": config.daily_tasks_silence_minutes,
                    })
                    return False
        return True

    def _dedup_wind_topics(self):
        """End-of-day topic deduplication via LLM."""
        if not self._wind_orchestrator:
            return
        try:
            self._wind_orchestrator.deduplicate_topics_all()
        except Exception as e:
            logger.warning("Scheduler: topic dedup failed", extra={"error": str(e)})

    def _rollup_moods(self) -> None:
        """Daily mood intensity decay toward moderate for all allowlisted conversations."""
        if not self._wind_orchestrator:
            return
        for conv_id in self._wind_orchestrator.config.allowlist:
            try:
                self._wind_orchestrator.state_manager.rollup_mood(conv_id)
            except Exception as e:
                logger.warning("Failed to rollup mood", extra={
                    "conversation_id": conv_id, "error": str(e),
                })

    def _rollup_feedback_decay(self) -> None:
        """Daily decay of Wind topic rejection weights (forgiveness over time)."""
        if not self._wind_orchestrator:
            return
        try:
            self._wind_orchestrator.feedback_manager.apply_daily_decay()
        except Exception as e:
            logger.warning("Failed to apply feedback decay", extra={"error": str(e)})

    def _startup_config_push(self):
        """Push config to mesh on startup to ensure sync."""
        if not self._config_push_client:
            return
        try:
            logger.info("Startup: pushing config to mesh...", extra={"action": "startup_config_push"})
            success, result = self._config_push_client.push_config(force=True)
            if success:
                logger.info("Startup: config push successful", extra={"config_hash": result[:16]})
            else:
                logger.warning("Startup: config push failed", extra={"error": result})
        except Exception as e:
            logger.warning("Startup: config push failed", extra={"error": str(e)})

    def get_status(self) -> dict:
        """Get scheduler status for health endpoint."""
        return {
            "running": self._running,
            "interval_seconds": self._interval,
            "tick_count": self._tick_count,
            "error_count": self._error_count,
            "last_tick": self._last_tick,
        }
