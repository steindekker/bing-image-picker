# Bing Image Picker (Anki Addon)

An Anki add-on with a **🖼** editor button that searches **Bing Images** for the
note's word and lets you pick from a 3×3 grid.

The chosen image is saved to your collection and written to a field.

<p align="center">
  <img src="docs/screenshot.png" alt="Screenshot" width="400">
</p>

On first use per note type, a dialog asks which field to search from, which to put
the image in, and append vs. overwrite.

The mapping is editable later via **Tools → Bing Image Picker**.

## Install

*Tools → Add-ons → Get Add-ons*, paste code `2112316511`.

From source, symlink into Anki's add-on dir and restart:

```sh
ln -s "$PWD" ~/.local/share/Anki2/addons21/bing_image_picker
```
