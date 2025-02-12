# Copyright 2024 NVIDIA Corporation.  All rights reserved.
#
# Please refer to the NVIDIA end user license agreement (EULA) associated
# with this source code for terms and conditions that govern your use of
# this software. Any use, reproduction, disclosure, or distribution of
# this software and related documentation outside the terms of the EULA
# is strictly prohibited.

try:
    from cuda.bindings import driver
except ImportError:
    from cuda import cuda as driver

import ctypes

from cuda.core.experimental import Device
from cuda.core.experimental._memory import Buffer, MemoryResource, SharedMempool
from cuda.core.experimental._utils import handle_return


class DummyDeviceMemoryResource(MemoryResource):
    def __init__(self, device):
        self.device = device

    def allocate(self, size, stream=None) -> Buffer:
        ptr = handle_return(driver.cuMemAlloc(size))
        return Buffer(ptr=ptr, size=size, mr=self)

    def deallocate(self, ptr, size, stream=None):
        handle_return(driver.cuMemFree(ptr))

    @property
    def is_device_accessible(self) -> bool:
        return True

    @property
    def is_host_accessible(self) -> bool:
        return False

    @property
    def device_id(self) -> int:
        return 0


class DummyHostMemoryResource(MemoryResource):
    def __init__(self):
        pass

    def allocate(self, size, stream=None) -> Buffer:
        # Allocate a ctypes buffer of size `size`
        ptr = (ctypes.c_byte * size)()
        return Buffer(ptr=ptr, size=size, mr=self)

    def deallocate(self, ptr, size, stream=None):
        # the memory is deallocated per the ctypes deallocation at garbage collection time
        pass

    @property
    def is_device_accessible(self) -> bool:
        return False

    @property
    def is_host_accessible(self) -> bool:
        return True

    @property
    def device_id(self) -> int:
        raise RuntimeError("the pinned memory resource is not bound to any GPU")


class DummyUnifiedMemoryResource(MemoryResource):
    def __init__(self, device):
        self.device = device

    def allocate(self, size, stream=None) -> Buffer:
        ptr = handle_return(driver.cuMemAllocManaged(size, driver.CUmemAttach_flags.CU_MEM_ATTACH_GLOBAL.value))
        return Buffer(ptr=ptr, size=size, mr=self)

    def deallocate(self, ptr, size, stream=None):
        handle_return(driver.cuMemFree(ptr))

    @property
    def is_device_accessible(self) -> bool:
        return True

    @property
    def is_host_accessible(self) -> bool:
        return True

    @property
    def device_id(self) -> int:
        return 0


class DummyPinnedMemoryResource(MemoryResource):
    def __init__(self, device):
        self.device = device

    def allocate(self, size, stream=None) -> Buffer:
        ptr = handle_return(driver.cuMemAllocHost(size))
        return Buffer(ptr=ptr, size=size, mr=self)

    def deallocate(self, ptr, size, stream=None):
        handle_return(driver.cuMemFreeHost(ptr))

    @property
    def is_device_accessible(self) -> bool:
        return True

    @property
    def is_host_accessible(self) -> bool:
        return True

    @property
    def device_id(self) -> int:
        raise RuntimeError("the pinned memory resource is not bound to any GPU")


def buffer_initialization(dummy_mr: MemoryResource):
    buffer = dummy_mr.allocate(size=1024)
    assert buffer.handle != 0
    assert buffer.size == 1024
    assert buffer.memory_resource == dummy_mr
    assert buffer.is_device_accessible == dummy_mr.is_device_accessible
    assert buffer.is_host_accessible == dummy_mr.is_host_accessible
    buffer.close()


def test_buffer_initialization():
    device = Device()
    device.set_current()
    buffer_initialization(DummyDeviceMemoryResource(device))
    buffer_initialization(DummyHostMemoryResource())
    buffer_initialization(DummyUnifiedMemoryResource(device))
    buffer_initialization(DummyPinnedMemoryResource(device))


def buffer_copy_to(dummy_mr: MemoryResource, device: Device, check=False):
    src_buffer = dummy_mr.allocate(size=1024)
    dst_buffer = dummy_mr.allocate(size=1024)
    stream = device.create_stream()

    if check:
        src_ptr = ctypes.cast(src_buffer.handle, ctypes.POINTER(ctypes.c_byte))
        for i in range(1024):
            src_ptr[i] = ctypes.c_byte(i)

    src_buffer.copy_to(dst_buffer, stream=stream)
    device.sync()

    if check:
        dst_ptr = ctypes.cast(dst_buffer.handle, ctypes.POINTER(ctypes.c_byte))

        for i in range(10):
            assert dst_ptr[i] == src_ptr[i]

    dst_buffer.close()
    src_buffer.close()


def test_buffer_copy_to():
    device = Device()
    device.set_current()
    buffer_copy_to(DummyDeviceMemoryResource(device), device)
    buffer_copy_to(DummyUnifiedMemoryResource(device), device)
    buffer_copy_to(DummyPinnedMemoryResource(device), device, check=True)


def buffer_copy_from(dummy_mr: MemoryResource, device, check=False):
    src_buffer = dummy_mr.allocate(size=1024)
    dst_buffer = dummy_mr.allocate(size=1024)
    stream = device.create_stream()

    if check:
        src_ptr = ctypes.cast(src_buffer.handle, ctypes.POINTER(ctypes.c_byte))
        for i in range(1024):
            src_ptr[i] = ctypes.c_byte(i)

    dst_buffer.copy_from(src_buffer, stream=stream)
    device.sync()

    if check:
        dst_ptr = ctypes.cast(dst_buffer.handle, ctypes.POINTER(ctypes.c_byte))

        for i in range(10):
            assert dst_ptr[i] == src_ptr[i]

    dst_buffer.close()
    src_buffer.close()


def test_buffer_copy_from():
    device = Device()
    device.set_current()
    buffer_copy_from(DummyDeviceMemoryResource(device), device)
    buffer_copy_from(DummyUnifiedMemoryResource(device), device)
    buffer_copy_from(DummyPinnedMemoryResource(device), device, check=True)


def buffer_close(dummy_mr: MemoryResource):
    buffer = dummy_mr.allocate(size=1024)
    buffer.close()
    assert buffer.handle == 0
    assert buffer.memory_resource is None


def test_buffer_close():
    device = Device()
    device.set_current()
    buffer_close(DummyDeviceMemoryResource(device))
    buffer_close(DummyHostMemoryResource())
    buffer_close(DummyUnifiedMemoryResource(device))
    buffer_close(DummyPinnedMemoryResource(device))


def test_shared_memory_resource():
    import multiprocessing

    def child_process(shared_handle, queue):
        try:
            device = Device()
            device.set_current()

            # Import the shared memory pool
            mr = SharedMempool(device.device_id, shared_handle=shared_handle)

            # Allocate and write to buffer
            buffer = mr.allocate(1024)
            ptr = ctypes.cast(buffer.handle, ctypes.POINTER(ctypes.c_byte))
            for i in range(1024):
                ptr[i] = ctypes.c_byte(i % 256)

            # Signal parent process that data is ready
            queue.put("Data written")

            # Wait for parent to read
            assert queue.get() == "Data read"

            buffer.close()

        except Exception as e:
            queue.put(e)
            raise

    def parent_process(shared_handle, queue):
        try:
            # Import the shared memory pool
            mr = SharedMempool(device.device_id, shared_handle=shared_handle)

            # Wait for child to write data
            assert queue.get() == "Data written"

            # Read and verify data
            buffer = mr.allocate(1024)
            ptr = ctypes.cast(buffer.handle, ctypes.POINTER(ctypes.c_byte))
            for i in range(1024):
                assert ptr[i] == ctypes.c_byte(i % 256), f"Mismatch at index {i}"

            # Signal child that we've read the data
            queue.put("Data read")

            buffer.close()

        except Exception as e:
            queue.put(e)
            raise

    # Initialize device
    device = Device()
    device.set_current()

    # Create shared memory pool
    pool_size = 1024 * 1024  # 1MB
    mr = SharedMempool(device.device_id, max_size=pool_size)

    # Test basic allocation
    buffer = mr.allocate(1024)
    assert buffer.handle != 0
    assert buffer.size == 1024
    assert buffer.memory_resource == mr
    assert buffer.is_device_accessible
    assert not buffer.is_host_accessible
    buffer.close()

    # Get shareable handle
    shareable_handle = mr.get_shareable_handle()
    assert shareable_handle != 0

    # Test cross-process sharing
    multiprocessing.set_start_method("spawn", force=True)
    queue = multiprocessing.Queue()

    # Create child process
    process = multiprocessing.Process(target=child_process, args=(shareable_handle, queue))
    process.start()

    # Run parent process logic
    parent_process(shareable_handle, queue)

    # Wait for child process to complete
    process.join(timeout=10)
    assert process.exitcode == 0, "Child process failed"

    # Check for any exceptions from the child process
    if not queue.empty():
        exception = queue.get()
        if isinstance(exception, Exception):
            raise exception
