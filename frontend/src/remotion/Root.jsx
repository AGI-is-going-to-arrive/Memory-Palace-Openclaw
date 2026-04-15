import React from 'react';
import {Composition, Folder} from 'remotion';
import {OpenClawStory} from './components/OpenClawStory.jsx';
import {defaultStoryProps, getStoryDurationInFrames} from './story-defaults.js';

const calculateMetadata = ({props, compositionFps}) => {
  const merged = {
    ...defaultStoryProps,
    ...(props ?? {}),
  };

  return {
    width: 1600,
    height: 900,
    fps: 30,
    durationInFrames: getStoryDurationInFrames(merged, compositionFps ?? 30),
    props: merged,
  };
};

export const RemotionRoot = () => {
  return (
    <Folder name="OpenClaw">
      <Composition
        id="OpenClawDocStory"
        component={OpenClawStory}
        width={1600}
        height={900}
        fps={30}
        durationInFrames={180}
        defaultProps={defaultStoryProps}
        calculateMetadata={calculateMetadata}
      />
    </Folder>
  );
};
