"""The single source of truth for FrameReviewer's colors.

Widgets otherwise follow Maya/Qt's native dark style; everything here is the
small set of accents the app paints itself (surface art, timeline, the side
panel's role buttons, annotation and onion-skin tints). Keep literal hex out of
the rest of the package -- add a named token here and reference it, so the
palette stays consistent and adjustable in one place.
"""
from __future__ import absolute_import

# -- greys and the Maya selection-blue accent --------------------------------
BG = '#2b2b2b'          # window background
PANEL = '#383838'       # raised panel / side column / cards
TRACK = '#2b2b2b'       # timeline track well
CONTROL = '#5d5d5d'     # buttons, spin boxes
CONTROL_HOVER = '#6a6a6a'
CONTROL_DOWN = '#4a4a4a'
FIELD = '#2e2e2e'       # read-only / input fields
BORDER = '#1e1e1e'
TEXT = '#c3c3c3'
TEXT_DIM = '#8a8a8a'
ACCENT = '#5285a6'      # Maya selection blue (active tool button, crop)
ACCENT_HI = '#6fa8cf'

# -- side-panel button roles (importance hierarchy) --------------------------
PRIMARY = '#4b555c'             # the one main action per pane
PRIMARY_HOVER = '#53616a'
PRIMARY_BORDER = '#596b77'
DANGER = '#554545'              # destructive actions
DANGER_HOVER = '#604b4b'
DANGER_TEXT = '#cbbbbb'
DANGER_BORDER = '#684b4b'
TOOLMODE_CHECKED = '#465159'    # an engaged tool-mode toggle
TOOLMODE_CHECKED_BORDER = '#596f7d'
WARNING = '#d98b4a'             # cautionary inline text

# -- drawing / annotation ----------------------------------------------------
CROP = ACCENT                   # crop rectangle
DRAW_DEFAULT = '#ef5350'        # default brush / stroke color
DRAW_SWATCHES = ('#ef5350', '#f2b84b', '#ffffff', '#49a7ff', '#66d17a')
SWATCH_BORDER = '#202020'
ANNOTATION = '#f2b84b'          # neutral fallback for empty annotation keys

# -- Notes Select transform handles ------------------------------------------
SELECTION = '#64c9ff'           # selection box, marquee, scale handles
HANDLE_ROTATE = '#f8c26b'       # the rotate handle
HANDLE_OUTLINE = '#162632'      # handle pen outline

# -- onion skin directional tint ---------------------------------------------
ONION_PREV = '#ff735c'          # warm: earlier keys
ONION_NEXT = '#54c9ed'          # cool: later keys

# -- timeline art ------------------------------------------------------------
WAVE = '#9a9a9a'                # waveform fill
THUMB_FRAME = '#1e1e1e'
