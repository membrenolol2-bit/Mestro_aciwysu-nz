"""
paint_from_clipboard.py

Takes the image currently on your clipboard and "draws" it on screen by
moving the mouse and dragging (like a pen), the way you would in MS Paint
or any other drawing program -- reproducing multiple colors, one layer
at a time.

HOW IT WORKS
------------
1. You copy an image to the clipboard (e.g. Ctrl+C on an image, or a
   screenshot tool that copies to clipboard).
2. You open your paint program and position its canvas on screen.
3. You calibrate the drawing area by hovering the mouse over the
   top-left corner of the canvas and pressing "1", then hovering over the
   bottom-right corner and pressing "2".
4. Press "å" to start. The script reduces your picture to a handful of
   dominant colors (see NUM_COLORS below) and processes them one at a
   time, largest area first:
     - It prints the color to use, e.g. "Set your pen to #7a1f1f".
     - You pick that color in your paint program's palette.
     - You press "å" again to draw that layer.
     - Once that layer is done, it prompts you for the next color, and
       so on, until all layers are drawn.
5. Press "ä" at any time to stop everything immediately.

IMPORTANT LIMITATIONS (please read)
------------------------------------
- The script cannot click your paint program's color palette for you
  (every program's palette UI is different and unreliable to automate),
  so YOU set the pen color each round; the script only supplies the
  hex code and does the drawing motion once you confirm.
- It approximates your picture with NUM_COLORS flat colors and simple
  horizontal strokes -- fine gradients/antialiasing won't be pixel
  perfect, but shapes and color regions will be recognizable.
- Reading images from the clipboard works out of the box on Windows and
  macOS. On Linux, Pillow's clipboard support needs `xclip` (X11) or
  `wl-paste` (Wayland) installed on your system.
- Global hotkeys ("å"/"ä"/"1"/"2") are provided by the `keyboard`
  library. On Linux this usually needs to be run with `sudo`. On
  Windows/macOS it should just work.
- Keep pyautogui's fail-safe on: slamming the mouse into a screen corner
  (0,0) will abort everything immediately, as a safety net.

SETUP
-----
    pip install pyautogui pillow keyboard

RUN
---
    python paint_from_clipboard.py      (on Linux: sudo python paint_from_clipboard.py)
"""

import sys
import threading
import time

import pyautogui
from PIL import Image, ImageGrab

try:
    import keyboard
except ImportError:
    print("Missing dependency. Install it with: pip install keyboard")
    sys.exit(1)


# ------------------------------------------------------------------
# CONFIG - tweak these if you want
# ------------------------------------------------------------------
MAX_RESOLUTION = 150     # max width/height (in "dots") the picture is downscaled to.
                         # Higher = more detail but much slower and more mouse movements.
                         # For timed games like Gartic Phone, try 50-70 instead.
NUM_COLORS = 8           # how many flat color layers to reduce the picture to.
                         # More = more faithful colors, but more manual pen-color changes.
                         # For timed games like Gartic Phone, try 3-4 instead.
BACKGROUND_WHITE_CUTOFF = 235  # a layer counts as "blank paper" (skipped) if every
                               # channel is >= this value. Lower it if your canvas
                               # background isn't pure white.
MERGE_THRESHOLD = 25     # colors closer than this (in RGB distance) get merged into
                         # one layer, so quantization artifacts don't create pointless
                         # duplicate "same color, switch pens again" steps.
MOVE_SPEED = 0.0         # seconds per mouse move segment (0 = as fast as possible)
STEP_SLEEP = 0.0         # extra delay between drawing rows, if your program needs it

pyautogui.FAILSAFE = True  # move mouse to a screen corner (0,0) to abort instantly
pyautogui.PAUSE = 0


# ------------------------------------------------------------------
# STATE
# ------------------------------------------------------------------
corner_top_left = None
corner_bottom_right = None
stop_event = threading.Event()
resume_event = threading.Event()   # set by pressing å again, to advance to the next layer
drawing_thread = None


def capture_top_left():
    global corner_top_left
    corner_top_left = pyautogui.position()
    print(f"[calibration] Top-left corner set to {corner_top_left}")


def capture_bottom_right():
    global corner_bottom_right
    corner_bottom_right = pyautogui.position()
    print(f"[calibration] Bottom-right corner set to {corner_bottom_right}")


def load_clipboard_image():
    img = ImageGrab.grabclipboard()
    if img is None:
        print("No image found on the clipboard. Copy an image first, then try again.")
        return None
    if isinstance(img, list):
        # Some platforms return a list of file paths instead of an Image
        if len(img) == 0:
            print("Clipboard didn't contain a usable image.")
            return None
        try:
            img = Image.open(img[0])
        except Exception as e:
            print(f"Couldn't open clipboard file as image: {e}")
            return None
    return img.convert("RGB")


def build_color_layers(img, canvas_w, canvas_h):
    """
    Downscale the image, reduce it to NUM_COLORS flat colors, and split it
    into layers -- one per color, largest area first. Each layer is a dict
    with the color's hex code and a list of screen-coordinate strokes
    (x_start, x_end, y).
    """
    w, h = img.size
    scale = MAX_RESOLUTION / max(w, h)
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    quant = img.quantize(colors=NUM_COLORS, method=Image.MEDIANCUT)
    palette = quant.getpalette()  # flat list [r,g,b, r,g,b, ...]
    counts = quant.getcolors(maxcolors=NUM_COLORS * 4)  # [(count, index), ...]

    px = quant.load()
    img_w, img_h = quant.size
    cell_w = canvas_w / img_w
    cell_h = canvas_h / img_h

    # sort by area, largest first
    counts.sort(key=lambda c: c[0], reverse=True)

    # drop background-ish colors up front
    fg_counts = []
    for count, idx in counts:
        r, g, b = palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]
        if r >= BACKGROUND_WHITE_CUTOFF and g >= BACKGROUND_WHITE_CUTOFF and b >= BACKGROUND_WHITE_CUTOFF:
            continue
        fg_counts.append((count, idx, (r, g, b)))

    # greedily cluster near-identical colors together (largest area first),
    # so quantization noise doesn't produce duplicate "same color again" layers
    clusters = []  # list of {"rgb": representative color, "indices": set(), "count": total}
    for count, idx, rgb in fg_counts:
        placed = False
        for cluster in clusters:
            cr, cg, cb = cluster["rgb"]
            dist = ((rgb[0] - cr) ** 2 + (rgb[1] - cg) ** 2 + (rgb[2] - cb) ** 2) ** 0.5
            if dist <= MERGE_THRESHOLD:
                cluster["indices"].add(idx)
                cluster["count"] += count
                placed = True
                break
        if not placed:
            clusters.append({"rgb": rgb, "indices": {idx}, "count": count})

    clusters.sort(key=lambda c: c["count"], reverse=True)

    layers = []
    for cluster in clusters:
        indices = cluster["indices"]
        r, g, b = cluster["rgb"]

        screen_strokes = []
        for y in range(img_h):
            run_start = None
            for x in range(img_w):
                match = px[x, y] in indices
                if match and run_start is None:
                    run_start = x
                elif not match and run_start is not None:
                    sx0 = corner_top_left[0] + run_start * cell_w + cell_w / 2
                    sx1 = corner_top_left[0] + (x - 1) * cell_w + cell_w / 2
                    sy = corner_top_left[1] + y * cell_h + cell_h / 2
                    screen_strokes.append((sx0, sx1, sy))
                    run_start = None
            if run_start is not None:
                sx0 = corner_top_left[0] + run_start * cell_w + cell_w / 2
                sx1 = corner_top_left[0] + (img_w - 1) * cell_w + cell_w / 2
                sy = corner_top_left[1] + y * cell_h + cell_h / 2
                screen_strokes.append((sx0, sx1, sy))

        if screen_strokes:
            layers.append({
                "hex": f"#{r:02x}{g:02x}{b:02x}",
                "strokes": screen_strokes,
            })
    return layers


def draw_strokes(strokes):
    for (x_start, x_end, y) in strokes:
        if stop_event.is_set():
            return False
        try:
            pyautogui.moveTo(x_start, y, duration=MOVE_SPEED)
            pyautogui.mouseDown()
            pyautogui.moveTo(x_end, y, duration=MOVE_SPEED)
            pyautogui.mouseUp()
        except pyautogui.FailSafeException:
            print("Fail-safe triggered (mouse hit screen corner). Aborting.")
            return False
        if STEP_SLEEP:
            time.sleep(STEP_SLEEP)
    return True


def draw_worker():
    if corner_top_left is None or corner_bottom_right is None:
        print("Please calibrate the canvas first: hover top-left corner and press '1', "
              "hover bottom-right corner and press '2'.")
        return

    img = load_clipboard_image()
    if img is None:
        return

    canvas_w = corner_bottom_right[0] - corner_top_left[0]
    canvas_h = corner_bottom_right[1] - corner_top_left[1]
    if canvas_w <= 0 or canvas_h <= 0:
        print("Bottom-right corner must be below and to the right of top-left corner.")
        return

    print("Analyzing image and splitting it into color layers...")
    layers = build_color_layers(img, canvas_w, canvas_h)
    if not layers:
        print("Nothing to draw (image looked blank after color reduction).")
        return

    print(f"Found {len(layers)} color layer(s).")

    # First layer: we already consumed the å press that triggered draw_worker,
    # so go straight into drawing it.
    for i, layer in enumerate(layers):
        print(f"\n--- Layer {i + 1}/{len(layers)}: color {layer['hex']} "
              f"({len(layer['strokes'])} strokes) ---")
        if i == 0:
            print("Set your pen/brush to this color now if it isn't already, then drawing...")
        else:
            print(f"Set your pen/brush color to {layer['hex']}, then press 'å' to draw this layer.")
            resume_event.clear()
            while not resume_event.is_set():
                if stop_event.is_set():
                    print("Stopped by user.")
                    return
                time.sleep(0.1)

        ok = draw_strokes(layer["strokes"])
        if not ok:
            print("Stopped by user.")
            return

    print("\nAll layers drawn. Done!")


def start_drawing():
    global drawing_thread
    if drawing_thread is not None and drawing_thread.is_alive():
        # We're already mid-drawing: this å press means "continue to next layer"
        resume_event.set()
        return
    stop_event.clear()
    resume_event.clear()
    drawing_thread = threading.Thread(target=draw_worker, daemon=True)
    drawing_thread.start()


def stop_drawing():
    if drawing_thread is not None and drawing_thread.is_alive():
        stop_event.set()
        print("Stop requested...")
    else:
        print("Not currently drawing.")


def main():
    print(__doc__)
    print("Ready.")
    print(" 1  -> set top-left corner of canvas (hover mouse there first)")
    print(" 2  -> set bottom-right corner of canvas (hover mouse there first)")
    print(" å  -> start drawing the clipboard image")
    print(" ä  -> stop drawing")
    print(" Ctrl+C in this terminal -> quit the script")

    keyboard.add_hotkey("1", capture_top_left)
    keyboard.add_hotkey("2", capture_bottom_right)
    keyboard.add_hotkey("å", start_drawing)
    keyboard.add_hotkey("ä", stop_drawing)

    try:
        keyboard.wait()  # blocks forever, hotkeys keep working in the background
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()