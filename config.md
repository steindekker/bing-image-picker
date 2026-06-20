## Bing Image Picker

Field mappings are configured per note type from the GUI — press **🖼** in the
editor (first use prompts setup) or open **Tools → Bing Image Picker**. You don't
need to edit this JSON by hand, but the format is:

```json
{
  "notetypes": {
    "My Note Type": {
      "source": "Word",     // field whose text is searched on Bing
      "target": "Image",    // field the chosen <img> is written to
      "mode": "append"      // "append" or "overwrite"
    }
  }
}
```
