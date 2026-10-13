# Template logos and automatic application

Open Channels / Groups → channel → Templates → a template.

- **Logo:** send a photo or PNG/JPEG document, up to 2 MB. Send a transparent PNG as a document to preserve its transparency.
- **Position:** opens movement arrows (5% steps), corner/center presets and exact X/Y input. Coordinates range from 0 to 100 across the available space: `0 0` is top left, `50 50` is center, `100 100` is bottom right. The logo stays inside the image.
- **Size:** enter any whole percentage from 1 to 100, or use −/+ in the position panel for 5% steps. The logo fits inside a box of that percentage of the image dimensions, preserving its aspect ratio.
- **Remove logo:** removes only the logo configuration; template text and buttons remain.
- **Video cover:** upload a photo to use as the template's video cover. Uploads use the existing daily cover allowance. Remove cover clears only this template setting.
- **Apply automatically:** selects this template as the channel default. Turning it off removes that default. Each channel has one automatic template, as before; existing defaults remain enabled after migration.

In the draft/scheduled post editor, open Template, select the destination and choose its default, no template, or another template belonging to that channel. Removing or switching a template renders again from the original draft; logos and template text do not accumulate.

Logos apply to ordinary photos and video **cover images**, including matching album items, in private previews and publication. The video file is never downloaded or encoded for logo processing. A video without a cover is sent unchanged; no frame is extracted from it. A post's own cover takes priority over the template cover. Explicitly removing a cover in the post editor also suppresses the template cover until a new cover is added or the content is replaced.

Text, round-video notes, animations, documents and audio retain their existing delivery behavior. Applying a template in the existing published-message editor remains a text/button operation; it does not apply a template cover or remove a logo already baked into a photo/cover.

Image processing uses the existing FFmpeg installation and temporary files, with at most two simultaneous branding operations. Photos/covers are limited to 20 MB per image, logos to 2 MB and output to 49 MB. The image-processing timeout is 180 seconds. These limits do not apply to the video file, which uses its original Telegram file ID. Preparation failure stops the send; it does not fall back to an unbranded photo/cover. Temporary files are removed after completion or failure.

Scheduled payloads preserve the logo/cover settings captured when scheduled, following existing template snapshot behavior. Previously saved logo payloads also use cover-only behavior for videos. Editing a scheduled post requires scheduling it again. Delivered snapshots record the photo and returned video-cover Telegram file IDs so reopening an existing publication preserves its actual media. Original draft file IDs remain unchanged.

No production database migration, polling restart, deployment or live Telegram verification was performed for this feature.
