# Movement Overhaul

Fluid movement for Borderlands 2.

- **Sprint** - tap to toggle, sprint in any direction, keep sprinting while shooting and aiming, no aiming slowdown.
- **Slide** - crouch while sprinting. Steer it, jump out of it to keep the speed, chain slide-jump-slide, land into a slide with crouch held. Slides stay faster than a sprint until they end. Slopes speed you up or slow you down.
- **Dash and jumps** - a stamina-charge dash on the ground and in the air, a double jump, and a double-tap-crouch fast fall. Charges show as a small bar above the XP bar (style and position are adjustable).
- **Air control** - steer in the air without losing the speed you jumped with.
- **Mantle** - hold jump into a ledge to climb over it.
- **Sounds** - original sound effects for slides, dashes, air jumps and mantles.

## Install

1. Install the [Willow2 Mod Manager (Python SDK)](https://github.com/bl-sdk/willow2-mod-manager) v3.x.
2. Put `movement_overhaul.sdkmod` in `Borderlands 2/sdk_mods/`.
3. Start the game and turn the mod on under **Mods**.
4. Dash is on **Left Alt** by default - rebind it under *Mods > Movement Overhaul > Keybinds*.

If you had the separate **Sliding** mod, turn it off - this includes it, and running both doubles every slide. The same goes for the older standalone Autorun, Slide Tuning, Stamina, Air Control, Mantle, Sprint Combat and Movement Audio mods.

## Co-op

Everyone in the game needs the mod enabled. Slides are simulated by the host, using each player's own slide settings.

## Your own sounds

Drop 16-bit PCM `.wav` files into `sdk_mods/movement_overhaul_sounds/<slide|dash|air_jump|mantle>/` and restart the game. They appear in *Options > Sounds*. That folder lives outside the mod, so updating the mod never touches it.

The sounds play through Windows rather than the game's audio engine, so the in-game volume sliders don't affect them - use the volume sliders in the mod's options.

## Credits

Sliding is juso's [Sliding](https://github.com/juso40/bl2sdk-mods), included with his uemath, tweens and coroutines libraries under the MIT license. See `LICENSE`.
