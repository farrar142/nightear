// AudioWorklet: 입력(보통 48kHz 모노)을 16kHz 16-bit PCM으로 다운샘플링해 100ms(1600샘플) 단위로 메인 스레드에 보낸다.
// 다운샘플링은 박스 평균(간이 저역통과) + 소수 비율 위상 누적으로 한다.
const TARGET_RATE = 16000;
const OUT_SAMPLES = 1600; // 100ms

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE; // 입력 샘플 / 출력 샘플
    this.acc = 0; // 현재 출력 샘플에 누적 중인 (가중) 합
    this.phase = this.ratio; // 현재 출력 샘플을 채우기 위해 더 필요한 입력 샘플 수
    this.out = new Int16Array(OUT_SAMPLES);
    this.outIdx = 0;
  }

  pushOut(value) {
    const clamped = Math.max(-1, Math.min(1, value));
    this.out[this.outIdx++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    if (this.outIdx === OUT_SAMPLES) {
      const copy = this.out.slice();
      this.port.postMessage(copy.buffer, [copy.buffer]);
      this.outIdx = 0;
    }
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      let sample = input[i];
      let weight = 1; // 이 입력 샘플 중 아직 소비하지 않은 비율
      while (weight > 0) {
        const take = Math.min(weight, this.phase);
        this.acc += sample * take;
        this.phase -= take;
        weight -= take;
        if (this.phase <= 1e-9) {
          this.pushOut(this.acc / this.ratio);
          this.acc = 0;
          this.phase = this.ratio;
        }
      }
    }
    return true;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
