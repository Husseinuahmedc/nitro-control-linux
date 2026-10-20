# Nitro Control Design

## Scene And Strategy

A laptop owner checks thermals beside a full-screen workload in a dim room;
a restrained dark surface avoids a glaring white utility window. One red
accent identifies active control, not decorative danger. Dark UI is chosen
for this scene rather than as a category default.

## Palette

Canonical tokens are OKLCH. Native Qt must convert these to sRGB QColor values
at the rendering boundary; do not replace them with an unrelated hex palette.

```css
:root {
  --background: oklch(0.170 0.000 0);
  --surface: oklch(0.225 0.000 0);
  --raised: oklch(0.280 0.000 0);
  --ink: oklch(0.950 0.000 0);
  --muted: oklch(0.740 0.005 0);
  --primary: oklch(0.650 0.160 0);
  --primary-fill: oklch(0.540 0.160 0);
  --accent: oklch(0.850 0.110 150);
  --warning: oklch(0.860 0.110 85);
}
```

## Typography And Layout

Native system sans, one family. Compact 14-16px labels, 26px window heading,
32-36px measured RPM, tabular numeric values where available. A single window
with a status/ownership bar, the paired CPU/GPU controls, global Auto/Max
actions and an expandable diagnostics area. No sidebar or nested cards.
The hardware pair may sit side by side and stack on narrow windows.

## Interaction

Standard accessible Qt buttons, sliders, numeric inputs and focus states.
Apply is explicit: moving a slider alone does not silently issue writes.
Firmware response and RPM observation are separate confirmation steps. No
spinning decorative fans or interpolated numeric sensor displays. The app
starts unprivileged and read-only if the controller is absent or unapproved.

## States

Connected/read-only/unsupported/disconnected/recovery-failed states each name
the cause and next action. Missing data uses an unavailable marker. Do not
display Auto merely because a connection failed. Experimental hardware
validation remains visibly unverified until a real validation record exists.
