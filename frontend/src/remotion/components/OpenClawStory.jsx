import React, {useMemo} from 'react';
import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';

const THEMES = {
  zh: {
    accent: '#c89a4c',
    accentSoft: 'rgba(200,154,76,0.2)',
    ink: '#3f2f20',
    inkSoft: '#6d5741',
    paper: '#f6efe5',
    panel: 'rgba(255,251,245,0.84)',
    captionLabel: '当前解说',
  },
  en: {
    accent: '#b8834f',
    accentSoft: 'rgba(184,131,79,0.2)',
    ink: '#3a2c1f',
    inkSoft: '#6d5b46',
    paper: '#f5ede2',
    panel: 'rgba(255,251,245,0.84)',
    captionLabel: 'Narration',
  },
};

const getCurrentCaption = (captions, currentTimeMs) => {
  if (!Array.isArray(captions)) {
    return null;
  }
  return captions.find((caption) => {
    const startMs = Number(caption.startMs || 0);
    const endMs = Number(caption.endMs || 0);
    return currentTimeMs >= startMs && currentTimeMs < endMs;
  }) || null;
};

const SceneMedia = ({scene, progress}) => {
  const scale = interpolate(
    progress,
    [0, 1],
    [Number(scene.zoomStart ?? scene.zoomFrom ?? 1), Number(scene.zoomEnd ?? scene.zoomTo ?? 1.06)],
  );
  const sharedStyle = {
    width: '100%',
    height: '100%',
    objectFit: scene.objectFit || 'cover',
    objectPosition: scene.objectPosition || 'center center',
    transform: `scale(${scale})`,
  };

  if (scene.mediaType === 'video') {
    return <OffthreadVideo src={staticFile(scene.assetPath)} style={sharedStyle} muted={scene.muted !== false} />;
  }

  return <Img src={staticFile(scene.assetPath)} style={sharedStyle} />;
};

const SceneCard = ({scene, chapterLabel, theme}) => {
  return (
    <div
      style={{
        width: 420,
        borderRadius: 28,
        padding: '24px 28px',
        background: theme.panel,
        border: `1px solid ${theme.accentSoft}`,
        boxShadow: '0 24px 60px rgba(77, 57, 35, 0.14)',
        backdropFilter: 'blur(14px)',
      }}
    >
      <div
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 10,
          borderRadius: 999,
          padding: '8px 14px',
          background: theme.accentSoft,
          color: theme.accent,
          fontSize: 18,
          fontWeight: 700,
          letterSpacing: '0.04em',
        }}
      >
        <span>{scene.shortLabel || scene.badge || ''}</span>
        <span style={{opacity: 0.65}}>·</span>
        <span>{chapterLabel}</span>
      </div>
      {scene.eyebrow || scene.badge ? (
        <div
          style={{
            marginTop: 18,
            color: theme.accent,
            fontSize: 18,
            fontWeight: 700,
            letterSpacing: '0.06em',
            textTransform: 'uppercase',
          }}
        >
          {scene.eyebrow || scene.badge}
        </div>
      ) : null}
      <div
        style={{
          marginTop: 12,
          color: theme.ink,
          fontSize: 38,
          lineHeight: 1.15,
          fontWeight: 700,
        }}
      >
        {scene.heading || scene.headline || ''}
      </div>
      {scene.body ? (
        <div
          style={{
            marginTop: 14,
            color: theme.inkSoft,
            fontSize: 22,
            lineHeight: 1.45,
            whiteSpace: 'pre-wrap',
          }}
        >
          {scene.body}
        </div>
      ) : null}
    </div>
  );
};

const SceneLayer = ({scene, title, subtitle, footerNote, chapterLabel, theme, from}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const localFrame = frame - from;
  const duration = Math.max(1, Number(scene.durationFrames) || Math.round((Number(scene.seconds) || 0) * fps) || 1);
  const progress = interpolate(localFrame, [0, Math.max(1, duration - 1)], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const entrance = spring({
    frame: localFrame,
    fps,
    config: {
      damping: 18,
      stiffness: 120,
      mass: 0.9,
    },
  });

  return (
    <AbsoluteFill>
      <SceneMedia scene={scene} progress={progress} />
      <AbsoluteFill
        style={{
          background: 'linear-gradient(180deg, rgba(27,20,15,0.08) 0%, rgba(27,20,15,0.18) 100%)',
        }}
      />
      <AbsoluteFill
        style={{
          padding: 48,
          display: 'flex',
          justifyContent: 'space-between',
        }}
      >
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
            transform: `translateY(${interpolate(entrance, [0, 1], [30, 0])}px)`,
            opacity: entrance,
          }}
        >
          <div
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 12,
              padding: '10px 16px',
              borderRadius: 999,
              background: theme.panel,
              color: theme.ink,
              border: `1px solid ${theme.accentSoft}`,
              fontSize: 18,
              fontWeight: 600,
              width: 'fit-content',
            }}
          >
            <span
              style={{
                width: 12,
                height: 12,
                borderRadius: 999,
                background: theme.accent,
                display: 'inline-block',
              }}
            />
            {title}
          </div>
          {subtitle ? (
            <div
              style={{
                padding: '10px 16px',
                borderRadius: 18,
                background: 'rgba(58,44,31,0.55)',
                color: '#fff8f0',
                fontSize: 20,
                lineHeight: 1.35,
                maxWidth: 720,
              }}
            >
              {subtitle}
            </div>
          ) : null}
        </div>

        {scene.showCard === false ? null : (
          <div
            style={{
              alignSelf: 'flex-end',
              transform: `translateY(${interpolate(entrance, [0, 1], [46, 0])}px)`,
              opacity: entrance,
            }}
          >
            <SceneCard scene={scene} chapterLabel={chapterLabel} theme={theme} />
          </div>
        )}
      </AbsoluteFill>
      {footerNote ? (
        <div
          style={{
            position: 'absolute',
            left: 48,
            bottom: 128,
            padding: '9px 14px',
            borderRadius: 999,
            background: 'rgba(255,255,255,0.76)',
            color: theme.inkSoft,
            border: `1px solid ${theme.accentSoft}`,
            fontSize: 16,
            fontWeight: 600,
          }}
        >
          {footerNote}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};

const CaptionOverlay = ({caption, theme}) => {
  if (!caption) {
    return null;
  }
  return (
    <div
      style={{
        position: 'absolute',
        left: 48,
        right: 48,
        bottom: 34,
        display: 'flex',
        justifyContent: 'center',
      }}
    >
      <div
        style={{
          maxWidth: 1240,
          padding: '18px 24px',
          borderRadius: 26,
          background: 'rgba(30,24,18,0.7)',
          color: '#fffaf5',
          boxShadow: '0 20px 60px rgba(34, 22, 11, 0.28)',
          border: `1px solid ${theme.accentSoft}`,
          backdropFilter: 'blur(18px)',
        }}
      >
        <div
          style={{
            color: theme.accent,
            fontSize: 16,
            fontWeight: 700,
            letterSpacing: '0.08em',
            textTransform: 'uppercase',
            marginBottom: 8,
          }}
        >
          {theme.captionLabel}
        </div>
        <div
          style={{
            fontSize: 28,
            lineHeight: 1.38,
            fontWeight: 600,
            whiteSpace: 'pre-wrap',
          }}
        >
          {caption}
        </div>
      </div>
    </div>
  );
};

export const OpenClawStory = ({
  title,
  subtitle,
  footerNote,
  chapterLabel,
  language,
  scenes,
  captions,
}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const theme = THEMES[language] || THEMES.en;
  const currentTimeMs = (frame / fps) * 1000;
  const activeCaption = getCurrentCaption(captions, currentTimeMs);

  const decoratedScenes = useMemo(() => {
    let cursor = 0;
    return (scenes || []).map((scene, index) => {
      const resolvedDurationFrames = Math.max(
        1,
        Number(scene.durationFrames) ||
          Math.round((Number(scene.seconds) || 0) * fps) ||
          1,
      );
      const from = cursor;
      cursor += resolvedDurationFrames;
      return {
        ...scene,
        id: scene.id || `scene-${index + 1}`,
        durationFrames: resolvedDurationFrames,
        from,
      };
    });
  }, [fps, scenes]);

  return (
    <AbsoluteFill
      style={{
        background: `radial-gradient(circle at top left, rgba(255,255,255,0.82), transparent 36%), linear-gradient(135deg, ${theme.paper}, #eadbc8 52%, #f6efe5 100%)`,
        fontFamily: 'Avenir Next, Inter, PingFang SC, Helvetica, Arial, sans-serif',
      }}
    >
      {decoratedScenes.map((scene) => {
        return (
          <Sequence key={scene.id} from={scene.from} durationInFrames={scene.durationFrames}>
            <SceneLayer
              scene={scene}
              title={title}
              subtitle={subtitle}
              footerNote={footerNote}
              chapterLabel={chapterLabel}
              theme={theme}
              from={scene.from}
            />
          </Sequence>
        );
      })}
      <CaptionOverlay caption={activeCaption?.text || ''} theme={theme} />
    </AbsoluteFill>
  );
};
