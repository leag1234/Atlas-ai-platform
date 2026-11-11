// Microphone capture for the dictation test page: mono, 16 kHz, 16-bit PCM frames of 50 ms.
// Averaging decimation from the context rate; each frame also carries its RMS level so the
// page can time the speech it hears.
class DictationCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.position = 0;
    this.sum = 0;
    this.count = 0;
    this.frame = new Int16Array(800);
    this.filled = 0;
    this.energy = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i += 1) {
      this.sum += channel[i];
      this.count += 1;
      this.position += 1;
      if (this.position >= this.ratio) {
        this.position -= this.ratio;
        const value = Math.max(-1, Math.min(1, this.sum / this.count));
        this.sum = 0;
        this.count = 0;
        this.energy += value * value;
        this.frame[this.filled] = value < 0 ? value * 0x8000 : value * 0x7fff;
        this.filled += 1;
        if (this.filled === this.frame.length) {
          const level = Math.sqrt(this.energy / this.filled);
          this.port.postMessage({ pcm: this.frame.buffer, level }, [this.frame.buffer]);
          this.frame = new Int16Array(800);
          this.filled = 0;
          this.energy = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('dictation-capture', DictationCapture);
