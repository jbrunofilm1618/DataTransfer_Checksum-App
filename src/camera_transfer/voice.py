"""Voice interface using macOS native Speech Recognition framework.

Uses Apple's SFSpeechRecognizer via PyObjC for on-device speech recognition
and NSSpeechSynthesizer for text-to-speech feedback.

Falls back to keyboard input if speech recognition is unavailable.
"""

import sys
import threading
import queue
from typing import Optional

# PyObjC imports — these only work on macOS
try:
    import objc
    import Speech
    import AVFoundation
    from Foundation import NSObject, NSRunLoop, NSDate, NSLocale

    SPEECH_AVAILABLE = True
except ImportError:
    SPEECH_AVAILABLE = False


class VoiceCommand:
    """Parsed voice command."""

    def __init__(self, raw_text: str):
        self.raw_text = raw_text.strip().lower()
        self.command = ""
        self.args: list[str] = []
        self._parse()

    def _parse(self):
        """Parse raw speech text into a command and arguments."""
        text = self.raw_text

        # Transfer commands
        if any(w in text for w in ["transfer", "copy", "move"]):
            self.command = "transfer"
            # Try to extract source and destination
            if "to" in text:
                parts = text.split("to", 1)
                self.args = [p.strip() for p in parts]
            elif "from" in text:
                parts = text.split("from", 1)
                self.args = [p.strip() for p in reversed(parts)]

        # List / show commands
        elif any(w in text for w in ["list", "show", "what"]):
            if any(w in text for w in ["volume", "drive", "disk"]):
                self.command = "list_volumes"
            elif any(w in text for w in ["card", "camera"]):
                self.command = "list_cameras"
            elif any(w in text for w in ["status", "progress"]):
                self.command = "status"
            else:
                self.command = "list_volumes"

        # Verify commands
        elif any(w in text for w in ["verify", "check", "checksum"]):
            self.command = "verify"

        # Stop / cancel
        elif any(w in text for w in ["stop", "cancel", "abort"]):
            self.command = "stop"

        # Help
        elif any(w in text for w in ["help", "what can"]):
            self.command = "help"

        # Quit
        elif any(w in text for w in ["quit", "exit", "bye"]):
            self.command = "quit"

        # Yes / confirm
        elif any(w in text for w in ["yes", "yeah", "confirm", "go ahead", "do it"]):
            self.command = "confirm"

        # No / deny
        elif any(w in text for w in ["no", "nope", "cancel", "never mind"]):
            self.command = "deny"

        else:
            self.command = "unknown"

    def __repr__(self):
        return f"VoiceCommand(command={self.command!r}, raw={self.raw_text!r})"


class VoiceInterface:
    """Manages speech recognition input and text-to-speech output.

    Falls back to terminal input/output when speech is unavailable.
    """

    def __init__(self):
        self.speech_available = SPEECH_AVAILABLE
        self._command_queue: queue.Queue[VoiceCommand] = queue.Queue()
        self._listening = False
        self._recognizer = None
        self._audio_engine = None
        self._request = None
        self._recognition_task = None
        self._synthesizer = None

        if self.speech_available:
            self._setup_speech()

    def _setup_speech(self):
        """Initialize macOS speech recognition and synthesis."""
        try:
            # Speech recognizer
            locale = NSLocale.localeWithLocaleIdentifier_("en-US")
            self._recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(locale)

            # Text-to-speech
            self._synthesizer = AVFoundation.AVSpeechSynthesizer.alloc().init()

        except Exception as e:
            print(f"Speech setup failed, falling back to keyboard: {e}")
            self.speech_available = False

    def request_authorization(self, callback=None):
        """Request speech recognition authorization from the user.

        On macOS, this triggers a system permission dialog.
        """
        if not self.speech_available:
            if callback:
                callback(False)
            return

        def auth_handler(status):
            authorized = (status == Speech.SFSpeechRecognizerAuthorizationStatusAuthorized)
            if callback:
                callback(authorized)

        Speech.SFSpeechRecognizer.requestAuthorization_(auth_handler)

    def start_listening(self):
        """Start continuous speech recognition.

        Recognized commands are placed in the command queue.
        """
        if not self.speech_available or not self._recognizer:
            return False

        if not self._recognizer.isAvailable():
            print("Speech recognizer not available")
            return False

        self._listening = True

        try:
            self._audio_engine = AVFoundation.AVAudioEngine.alloc().init()
            self._request = Speech.SFSpeechAudioBufferRecognitionRequest.alloc().init()
            self._request.setShouldReportPartialResults_(False)

            input_node = self._audio_engine.inputNode()
            record_format = input_node.outputFormatForBus_(0)

            def recognition_handler(result, error):
                if error:
                    return
                if result and result.isFinal():
                    text = result.bestTranscription().formattedString()
                    cmd = VoiceCommand(text)
                    self._command_queue.put(cmd)

            self._recognition_task = self._recognizer.recognitionTaskWithRequest_resultHandler_(
                self._request, recognition_handler
            )

            def tap_block(buffer, when):
                self._request.appendAudioPCMBuffer_(buffer)

            input_node.installTapOnBus_bufferSize_format_block_(
                0, 1024, record_format, tap_block
            )

            self._audio_engine.prepare()
            self._audio_engine.startAndReturnError_(None)

            return True

        except Exception as e:
            print(f"Failed to start listening: {e}")
            self._listening = False
            return False

    def stop_listening(self):
        """Stop speech recognition."""
        self._listening = False

        if self._audio_engine:
            self._audio_engine.stop()
            input_node = self._audio_engine.inputNode()
            input_node.removeTapOnBus_(0)

        if self._request:
            self._request.endAudio()

        if self._recognition_task:
            self._recognition_task.cancel()

    def get_command(self, timeout: float = 0.5) -> Optional[VoiceCommand]:
        """Get the next voice command from the queue.

        Returns None if no command is available within the timeout.
        """
        try:
            return self._command_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def speak(self, text: str):
        """Speak text aloud using macOS text-to-speech."""
        if self.speech_available and self._synthesizer:
            try:
                utterance = AVFoundation.AVSpeechUtterance.speechUtteranceWithString_(text)
                utterance.setRate_(0.5)
                utterance.setVoice_(
                    AVFoundation.AVSpeechSynthesisVoice.voiceWithLanguage_("en-US")
                )
                self._synthesizer.speakUtterance_(utterance)
            except Exception:
                # Fall back to print
                pass

        # Always print to terminal as well
        print(f"[Voice] {text}")

    def get_keyboard_command(self, prompt: str = "> ") -> VoiceCommand:
        """Get a command from keyboard input (fallback mode)."""
        try:
            text = input(prompt)
            return VoiceCommand(text)
        except (EOFError, KeyboardInterrupt):
            return VoiceCommand("quit")

    @property
    def is_listening(self) -> bool:
        return self._listening
