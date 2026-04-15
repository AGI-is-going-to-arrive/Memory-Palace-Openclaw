export const defaultStoryProps = {
  language: 'en',
  width: 1920,
  height: 1080,
  fps: 30,
  title: 'OpenClaw Memory Palace',
  subtitle: 'Remotion preview story',
  eyebrow: 'OpenClaw WebUI',
  languageStyle: {},
  scenes: [
    {
      mediaType: 'image',
      assetPath: 'remotion/openclaw/placeholder.svg',
      seconds: 3,
      headline: 'Preview scene',
      body: 'Replace this scene with synchronized E2E-captured assets and production captions.',
      badge: 'placeholder',
      zoomStart: 1,
      zoomEnd: 1.06,
    },
  ],
  captions: [
    {
      text: 'Replace this placeholder caption with generated story captions.',
      startMs: 0,
      endMs: 3000,
      timestampMs: 0,
      confidence: 1,
    },
  ],
};

export const getStoryDurationInFrames = (props, fps) => {
  const scenes = Array.isArray(props?.scenes) ? props.scenes : [];
  const sceneDuration = scenes.reduce((sum, scene) => {
    const seconds = Number(scene?.seconds || 0);
    return sum + (Number.isFinite(seconds) && seconds > 0 ? seconds : 0);
  }, 0);
  const totalSeconds = sceneDuration > 0 ? sceneDuration + 0.8 : 4;
  return Math.max(Math.ceil(totalSeconds * fps), fps * 2);
};
