Screw AutoCut and their stupid monthly subscription for such a simple application.

SilenceCut is a desktop tool that automatically removes silence from video or audio files and generates a Final Cut Pro XML timeline with only the meaningful content preserved.
Designed for fast cleanup of recordings, voiceovers, and gameplay footage before bringing them into an editor.

It lets you:
- Preview your media with embedded playback
- Visually inspect kept vs cut sections on a timeline
- Tune silence detection parameters (threshold, padding, minimum keep time)
- Jump between cuts, scrub, and zoom the timeline like a real editor
- Reopen the app and continue exactly where you left off

This isn't super well documented and I'm not planning on supporting it too well so good luck with everything, but just run the build.bat and open the built .exe in /dist
You need VLC (MAKE SURE IT IS THE x64 VERSION, I MEAN IT) and python-vlc to build it. 

Usage:
1. Browse for a video file
2. Analyze it
3. When you're happy with the cut segments hit Generate XML
4. Import the generated XML into premiere, it'll automatically detect it as a sequence
5. Enjoy! :)

<img width="1438" height="802" alt="image" src="https://github.com/user-attachments/assets/734435b6-b167-4abb-9c12-40b23629e5c6" />
