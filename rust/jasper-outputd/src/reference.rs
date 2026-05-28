//! Bounded speaker-reference fanout.
//!
//! Side consumers are intentionally lossy. The DAC path publishes the
//! newest mixed period, and any consumer that has not drained its queue
//! fast enough drops the oldest queued packet before receiving the new
//! one. Sequence numbers make that damage visible. The publish path
//! copies into preallocated per-consumer slots; allocations happen at
//! setup time or on the consumer/drain side, not while writing the DAC.

use crate::types::{AudioFormat, ReferencePacket};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ConsumerId(usize);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConsumerStats {
    pub name: String,
    pub queued_packets: usize,
    pub dropped_packets: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PublishedReference {
    pub stream_id: u64,
    pub sequence: u64,
    pub frame_count: u32,
    pub clipped_samples: u32,
}

pub struct ReferenceFanout {
    stream_id: u64,
    next_sequence: u64,
    format: AudioFormat,
    max_samples_per_packet: usize,
    consumers: Vec<ReferenceConsumer>,
}

struct ReferenceConsumer {
    name: String,
    queue: BoundedReferenceQueue,
    dropped_packets: u64,
}

impl ReferenceFanout {
    pub fn new(stream_id: u64, format: AudioFormat, max_frame_count: u32) -> Self {
        Self {
            stream_id,
            next_sequence: 0,
            format,
            max_samples_per_packet: format.samples_for_frames(max_frame_count),
            consumers: Vec::new(),
        }
    }

    pub fn add_consumer(&mut self, name: impl Into<String>, capacity_packets: usize) -> ConsumerId {
        assert!(capacity_packets > 0, "reference capacity must be > 0");
        let id = ConsumerId(self.consumers.len());
        self.consumers.push(ReferenceConsumer {
            name: name.into(),
            queue: BoundedReferenceQueue::new(
                capacity_packets,
                self.max_samples_per_packet,
                self.format,
            ),
            dropped_packets: 0,
        });
        id
    }

    pub fn publish(
        &mut self,
        samples: &[i16],
        frame_count: u32,
        clipped_samples: u32,
        monotonic_ns: u64,
    ) -> PublishedReference {
        assert_eq!(samples.len(), self.format.samples_for_frames(frame_count));
        assert!(samples.len() <= self.max_samples_per_packet);

        let sequence = self.next_sequence;
        self.next_sequence += 1;

        for consumer in &mut self.consumers {
            if consumer.queue.push(
                self.stream_id,
                sequence,
                monotonic_ns,
                frame_count,
                clipped_samples,
                samples,
            ) {
                consumer.dropped_packets += 1;
            }
        }

        PublishedReference {
            stream_id: self.stream_id,
            sequence,
            frame_count,
            clipped_samples,
        }
    }

    pub fn drain_consumer(&mut self, id: ConsumerId) -> Vec<ReferencePacket> {
        self.consumers[id.0].queue.drain_all()
    }

    pub fn consumer_stats(&self, id: ConsumerId) -> ConsumerStats {
        let consumer = &self.consumers[id.0];
        ConsumerStats {
            name: consumer.name.clone(),
            queued_packets: consumer.queue.len(),
            dropped_packets: consumer.dropped_packets,
        }
    }
}

struct BoundedReferenceQueue {
    slots: Vec<ReferenceSlot>,
    head: usize,
    len: usize,
    format: AudioFormat,
}

impl BoundedReferenceQueue {
    fn new(capacity: usize, max_samples: usize, format: AudioFormat) -> Self {
        let slots = (0..capacity)
            .map(|_| ReferenceSlot::new(max_samples))
            .collect();
        Self {
            slots,
            head: 0,
            len: 0,
            format,
        }
    }

    fn len(&self) -> usize {
        self.len
    }

    fn push(
        &mut self,
        stream_id: u64,
        sequence: u64,
        monotonic_ns: u64,
        frame_count: u32,
        clipped_samples: u32,
        samples: &[i16],
    ) -> bool {
        let dropped = self.len == self.slots.len();
        let write_index = if dropped {
            let idx = self.head;
            self.head = (self.head + 1) % self.slots.len();
            idx
        } else {
            let idx = (self.head + self.len) % self.slots.len();
            self.len += 1;
            idx
        };
        self.slots[write_index].write(
            stream_id,
            sequence,
            monotonic_ns,
            frame_count,
            clipped_samples,
            samples,
        );
        dropped
    }

    fn drain_all(&mut self) -> Vec<ReferencePacket> {
        let mut out = Vec::with_capacity(self.len);
        for offset in 0..self.len {
            let idx = (self.head + offset) % self.slots.len();
            out.push(self.slots[idx].to_packet(self.format));
        }
        self.head = 0;
        self.len = 0;
        out
    }
}

struct ReferenceSlot {
    stream_id: u64,
    sequence: u64,
    monotonic_ns: u64,
    frame_count: u32,
    clipped_samples: u32,
    samples_len: usize,
    samples: Vec<i16>,
}

impl ReferenceSlot {
    fn new(max_samples: usize) -> Self {
        Self {
            stream_id: 0,
            sequence: 0,
            monotonic_ns: 0,
            frame_count: 0,
            clipped_samples: 0,
            samples_len: 0,
            samples: vec![0; max_samples],
        }
    }

    fn write(
        &mut self,
        stream_id: u64,
        sequence: u64,
        monotonic_ns: u64,
        frame_count: u32,
        clipped_samples: u32,
        samples: &[i16],
    ) {
        self.stream_id = stream_id;
        self.sequence = sequence;
        self.monotonic_ns = monotonic_ns;
        self.frame_count = frame_count;
        self.clipped_samples = clipped_samples;
        self.samples_len = samples.len();
        self.samples[..samples.len()].copy_from_slice(samples);
    }

    fn to_packet(&self, format: AudioFormat) -> ReferencePacket {
        ReferencePacket {
            stream_id: self.stream_id,
            sequence: self.sequence,
            monotonic_ns: self.monotonic_ns,
            format,
            frame_count: self.frame_count,
            clipped_samples: self.clipped_samples,
            samples: self.samples[..self.samples_len].to_vec(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::AudioFormat;

    #[test]
    fn empty_consumer_drains_to_empty_vec() {
        let mut fanout = ReferenceFanout::new(7, AudioFormat::default(), 2);
        let consumer = fanout.add_consumer("aec", 2);

        assert!(fanout.drain_consumer(consumer).is_empty());
        assert_eq!(fanout.consumer_stats(consumer).queued_packets, 0);
    }

    #[test]
    fn full_consumer_drops_oldest_and_sequence_gap_is_visible() {
        let mut fanout = ReferenceFanout::new(7, AudioFormat::default(), 2);
        let consumer = fanout.add_consumer("aec", 2);
        let samples = vec![0i16; 4];

        fanout.publish(&samples, 2, 0, 10);
        fanout.publish(&samples, 2, 0, 20);
        fanout.publish(&samples, 2, 0, 30);

        let packets = fanout.drain_consumer(consumer);
        let sequences: Vec<u64> = packets.iter().map(|p| p.sequence).collect();
        assert_eq!(sequences, vec![1, 2]);
        assert_eq!(fanout.consumer_stats(consumer).dropped_packets, 1);
    }

    #[test]
    fn publish_without_consumers_still_advances_sequence() {
        let mut fanout = ReferenceFanout::new(7, AudioFormat::default(), 2);
        let samples = vec![0i16; 4];

        let first = fanout.publish(&samples, 2, 0, 10);
        let second = fanout.publish(&samples, 2, 0, 20);

        assert_eq!(first.sequence, 0);
        assert_eq!(second.sequence, 1);
    }
}
