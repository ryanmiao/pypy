import py
from pypy.rpython.lltypesystem import lltype, llmemory, llarena, llgroup, rffi
from pypy.rpython.memory.gc.stmgc import StmGC, WORD
from pypy.rpython.memory.gc.stmgc import GCFLAG_GLOBAL, GCFLAG_WAS_COPIED
from pypy.rpython.memory.support import mangle_hash


S = lltype.GcStruct('S', ('a', lltype.Signed), ('b', lltype.Signed),
                         ('c', lltype.Signed))
ofs_a = llmemory.offsetof(S, 'a')

SR = lltype.GcForwardReference()
SR.become(lltype.GcStruct('SR', ('s1', lltype.Ptr(S)),
                                ('sr2', lltype.Ptr(SR)),
                                ('sr3', lltype.Ptr(SR))))

WR = lltype.GcStruct('WeakRef', ('wadr', llmemory.Address))
SWR = lltype.GcStruct('SWR', ('wr', lltype.Ptr(WR)))


class FakeStmOperations:
    # The point of this class is to make sure about the distinction between
    # RPython code in the GC versus C code in translator/stm/src_stm.  This
    # class contains a fake implementation of what should be in C.  So almost
    # any use of 'self._gc' is wrong here: it's stmgc.py that should call
    # et.c, and not the other way around.

    PRIMITIVE_SIZES = {1: lltype.Char,
                       WORD: lltype.Signed}
    CALLBACK_ENUM = lltype.Ptr(lltype.FuncType([llmemory.Address] * 3,
                                               lltype.Void))
    GETSIZE  = lltype.Ptr(lltype.FuncType([llmemory.Address], lltype.Signed))

    threadnum = 0          # 0 = main thread; 1,2,3... = transactional threads

    def setup_size_getter(self, getsize_fn):
        self._getsize_fn = getsize_fn

    def in_transaction(self):
        return self.threadnum != 0

    def set_tls(self, tls, in_main_thread):
        assert lltype.typeOf(tls) == llmemory.Address
        assert tls
        if self.threadnum == 0:
            assert in_main_thread == 1
            assert not hasattr(self, '_tls_dict')
            self._tls_dict = {0: tls}
            self._tldicts = {0: {}}
            self._transactional_copies = []
        else:
            assert in_main_thread == 0
            self._tls_dict[self.threadnum] = tls
            self._tldicts[self.threadnum] = {}

    def get_tls(self):
        return self._tls_dict[self.threadnum]

    def del_tls(self):
        del self._tls_dict[self.threadnum]
        del self._tldicts[self.threadnum]

    def tldict_lookup(self, obj):
        assert lltype.typeOf(obj) == llmemory.Address
        assert obj
        tldict = self._tldicts[self.threadnum]
        return tldict.get(obj, llmemory.NULL)

    def tldict_add(self, obj, localobj):
        assert lltype.typeOf(obj) == llmemory.Address
        assert lltype.typeOf(localobj) == llmemory.Address
        tldict = self._tldicts[self.threadnum]
        assert obj not in tldict
        tldict[obj] = localobj

    def tldict_enum(self, callback):
        assert lltype.typeOf(callback) == self.CALLBACK_ENUM
        tls = self.get_tls()
        for key, value in self._tldicts[self.threadnum].iteritems():
            callback(tls, key, value)

    def _get_stm_reader(size, TYPE):
        realsize = rffi.sizeof(TYPE)
        assert size in (realsize, '%df' % realsize)
        PTYPE = rffi.CArrayPtr(TYPE)
        def stm_reader(self, obj, offset):
            hdr = self._gc.header(obj)
            if hdr.tid & GCFLAG_WAS_COPIED != 0:
                localobj = self.tldict_lookup(obj)
                if localobj:
                    assert self._gc.header(localobj).tid & GCFLAG_GLOBAL == 0
                    adr = rffi.cast(PTYPE, localobj + offset)
                    return adr[0]
            return 'stm_ll_read_int%s(%r, %r)' % (size, obj, offset)
        return stm_reader

    for _size, _TYPE in PRIMITIVE_SIZES.items():
        _func = _get_stm_reader(_size, _TYPE)
        locals()['stm_read_int%s' % _size] = _func

    def stm_copy_transactional_to_raw(self, srcobj, dstobj, size):
        llmemory.raw_memcopy(srcobj, dstobj, size)
        self._transactional_copies.append((srcobj, dstobj))


def fake_get_size(obj):
    TYPE = obj.ptr._TYPE.TO
    if isinstance(TYPE, lltype.GcStruct):
        return llmemory.sizeof(TYPE)
    else:
        assert 0

def fake_trace(obj, callback, arg):
    TYPE = obj.ptr._TYPE.TO
    if TYPE == S:
        ofslist = []     # no pointers in S
    elif TYPE == SR:
        ofslist = [llmemory.offsetof(SR, 's1'),
                   llmemory.offsetof(SR, 'sr2'),
                   llmemory.offsetof(SR, 'sr3')]
    elif TYPE == WR:
        ofslist = []
    elif TYPE == SWR:
        ofslist = [llmemory.offsetof(SWR, 'wr')]
    else:
        assert 0
    for ofs in ofslist:
        addr = obj + ofs
        if addr.address[0]:
            callback(addr, arg)

def fake_weakpointer_offset(tid):
    return llmemory.offsetof(WR, 'wadr')


class TestBasic:
    GCClass = StmGC

    def setup_method(self, meth):
        from pypy.config.pypyoption import get_pypy_config
        config = get_pypy_config(translating=True).translation
        self.gc = self.GCClass(config, FakeStmOperations(),
                               translated_to_c=False)
        self.gc.stm_operations._gc = self.gc
        self.gc.DEBUG = True
        self.gc.get_size = fake_get_size
        self.gc.trace = fake_trace
        self.gc.weakpointer_offset = fake_weakpointer_offset
        self.gc.setup()

    def teardown_method(self, meth):
        if not hasattr(self, 'gc'):
            return
        for key in self.gc.stm_operations._tls_dict.keys():
            if key != 0:
                self.gc.stm_operations.threadnum = key
                self.gc.teardown_thread()

    # ----------
    # test helpers
    def malloc(self, STRUCT, weakref=False):
        size = llarena.round_up_for_allocation(llmemory.sizeof(STRUCT))
        tid = lltype.cast_primitive(llgroup.HALFWORD, 123)
        gcref = self.gc.malloc_fixedsize_clear(tid, size,
                                               contains_weakptr=weakref)
        realobj = lltype.cast_opaque_ptr(lltype.Ptr(STRUCT), gcref)
        addr = llmemory.cast_ptr_to_adr(realobj)
        return realobj, addr
    def select_thread(self, threadnum):
        self.gc.stm_operations.threadnum = threadnum
        if threadnum not in self.gc.stm_operations._tls_dict:
            self.gc.setup_thread(False)
            self.gc.start_transaction()
    def gcsize(self, S):
        return (llmemory.raw_malloc_usage(llmemory.sizeof(self.gc.HDR)) +
                llmemory.raw_malloc_usage(llmemory.sizeof(S)))
    def checkflags(self, obj, must_have_global, must_have_was_copied,
                              must_have_version='?'):
        if lltype.typeOf(obj) != llmemory.Address:
            obj = llmemory.cast_ptr_to_adr(obj)
        hdr = self.gc.header(obj)
        assert (hdr.tid & GCFLAG_GLOBAL != 0) == must_have_global
        assert (hdr.tid & GCFLAG_WAS_COPIED != 0) == must_have_was_copied
        if must_have_version != '?':
            assert hdr.version == must_have_version
    def read_signed(self, obj, offset):
        meth = getattr(self.gc, 'read_int%d' % WORD)
        return meth(obj, offset)

    def test_gc_creation_works(self):
        pass

    def test_allocate_bump_pointer(self):
        a3 = self.gc.allocate_bump_pointer(3)
        a4 = self.gc.allocate_bump_pointer(4)
        a5 = self.gc.allocate_bump_pointer(5)
        a6 = self.gc.allocate_bump_pointer(6)
        assert a4 - a3 == 3
        assert a5 - a4 == 4
        assert a6 - a5 == 5

    def test_malloc_fixedsize_clear(self):
        gcref = self.gc.malloc_fixedsize_clear(123, llmemory.sizeof(S))
        s = lltype.cast_opaque_ptr(lltype.Ptr(S), gcref)
        assert s.a == 0
        assert s.b == 0
        gcref2 = self.gc.malloc_fixedsize_clear(123, llmemory.sizeof(S))
        assert gcref2 != gcref

    def test_malloc_main_vs_thread(self):
        gcref = self.gc.malloc_fixedsize_clear(123, llmemory.sizeof(S))
        obj = llmemory.cast_ptr_to_adr(gcref)
        assert self.gc.header(obj).tid & GCFLAG_GLOBAL != 0
        #
        self.select_thread(1)
        gcref = self.gc.malloc_fixedsize_clear(123, llmemory.sizeof(S))
        obj = llmemory.cast_ptr_to_adr(gcref)
        assert self.gc.header(obj).tid & GCFLAG_GLOBAL == 0

    def test_reader_direct(self):
        py.test.skip("xxx")
        s, s_adr = self.malloc(S)
        assert self.gc.header(s_adr).tid & GCFLAG_GLOBAL != 0
        s.a = 42
        value = self.read_signed(s_adr, ofs_a)
        assert value == 'stm_ll_read_int%d(%r, %r)' % (WORD, s_adr, ofs_a)
        #
        self.select_thread(1)
        s, s_adr = self.malloc(S)
        assert self.gc.header(s_adr).tid & GCFLAG_GLOBAL == 0
        self.gc.header(s_adr).tid |= GCFLAG_WAS_COPIED   # should be ignored
        s.a = 42
        value = self.read_signed(s_adr, ofs_a)
        assert value == 42

    def test_reader_through_dict(self):
        py.test.skip("xxx")
        s, s_adr = self.malloc(S)
        s.a = 42
        #
        self.select_thread(1)
        t, t_adr = self.malloc(S)
        t.a = 84
        #
        self.gc.header(s_adr).tid |= GCFLAG_WAS_COPIED
        self.gc.stm_operations._tldicts[1][s_adr] = t_adr
        #
        value = self.read_signed(s_adr, ofs_a)
        assert value == 84

    def test_reader_sizes(self):
        py.test.skip("xxx")
        for size, TYPE in PRIMITIVE_SIZES.items():
            T = lltype.GcStruct('T', ('a', TYPE))
            ofs_a = llmemory.offsetof(T, 'a')
            #
            self.select_thread(0)
            t, t_adr = self.malloc(T)
            assert self.gc.header(t_adr).tid & GCFLAG_GLOBAL != 0
            t.a = lltype.cast_primitive(TYPE, 42)
            #
            value = getattr(self.gc, 'read_int%d' % size)(t_adr, ofs_a)
            assert value == 'stm_ll_read_int%d(%r, %r)' % (size, t_adr, ofs_a)
            #
            self.select_thread(1)
            t, t_adr = self.malloc(T)
            assert self.gc.header(t_adr).tid & GCFLAG_GLOBAL == 0
            t.a = lltype.cast_primitive(TYPE, 42)
            value = getattr(self.gc, 'read_int%d' % size)(t_adr, ofs_a)
            assert lltype.typeOf(value) == TYPE
            assert lltype.cast_primitive(lltype.Signed, value) == 42

    def test_write_barrier_exists(self):
        self.select_thread(1)
        t, t_adr = self.malloc(S)
        obj = self.gc.stm_writebarrier(t_adr)     # local object
        assert obj == t_adr
        #
        self.select_thread(0)
        s, s_adr = self.malloc(S)
        #
        self.select_thread(1)
        self.gc.header(s_adr).tid |= GCFLAG_WAS_COPIED
        self.gc.header(t_adr).tid |= GCFLAG_WAS_COPIED
        self.gc.stm_operations._tldicts[1][s_adr] = t_adr
        obj = self.gc.stm_writebarrier(s_adr)     # global copied object
        assert obj == t_adr
        assert self.gc.stm_operations._transactional_copies == []

    def test_write_barrier_new(self):
        self.select_thread(0)
        s, s_adr = self.malloc(S)
        s.a = 12
        s.b = 34
        #
        self.select_thread(1)                # global object, not copied so far
        t_adr = self.gc.stm_writebarrier(s_adr)
        assert t_adr != s_adr
        t = t_adr.ptr
        assert t.a == 12
        assert t.b == 34
        assert self.gc.stm_operations._transactional_copies == [(s_adr, t_adr)]
        #
        u_adr = self.gc.stm_writebarrier(s_adr)  # again
        assert u_adr == t_adr
        #
        u_adr = self.gc.stm_writebarrier(u_adr)  # local object
        assert u_adr == t_adr

    def test_write_barrier_main_thread(self):
        t, t_adr = self.malloc(S)
        obj = self.gc.stm_writebarrier(t_adr)     # main thread
        assert obj == t_adr

    def test_commit_transaction_empty(self):
        self.select_thread(1)
        s, s_adr = self.malloc(S)
        t, t_adr = self.malloc(S)
        self.gc.collector.commit_transaction()    # no roots
        main_tls = self.gc.main_thread_tls
        assert main_tls.nursery_free == main_tls.nursery_start   # empty

    def test_commit_transaction_no_references(self):
        s, s_adr = self.malloc(S)
        s.b = 12345
        self.select_thread(1)
        t_adr = self.gc.stm_writebarrier(s_adr)   # make a local copy
        t = llmemory.cast_adr_to_ptr(t_adr, lltype.Ptr(S))
        assert s != t
        assert self.gc.header(t_adr).version == s_adr
        t.b = 67890
        #
        main_tls = self.gc.main_thread_tls
        assert main_tls.nursery_free != main_tls.nursery_start  # contains s
        old_value = main_tls.nursery_free
        #
        self.gc.collector.commit_transaction()
        #
        assert main_tls.nursery_free == old_value    # no new object
        assert s.b == 12345     # not updated by the GC code
        assert t.b == 67890     # still valid

    def _commit_transaction_with_one_reference(self, tls_page_size):
        self.gc.tls_page_size = tls_page_size
        #
        sr, sr_adr = self.malloc(SR)
        assert sr.s1 == lltype.nullptr(S)
        assert sr.sr2 == lltype.nullptr(SR)
        self.select_thread(1)
        tr_adr = self.gc.stm_writebarrier(sr_adr)   # make a local copy
        tr = llmemory.cast_adr_to_ptr(tr_adr, lltype.Ptr(SR))
        assert sr != tr
        t, t_adr = self.malloc(S)
        t.b = 67890
        assert tr.s1 == lltype.nullptr(S)
        assert tr.sr2 == lltype.nullptr(SR)
        tr.s1 = t
        #
        main_tls = self.gc.main_thread_tls
        old_value = main_tls.nursery_free
        #
        self.gc.collector.commit_transaction()
        #
        consumed = main_tls.nursery_free - old_value
        expected = self.gcsize(S)        # round this value up to tls_page_size
        if expected < tls_page_size: expected = tls_page_size
        assert consumed == expected

    def test_commit_transaction_with_one_reference_1(self):
        self._commit_transaction_with_one_reference(1)

    def test_commit_transaction_with_one_reference_N1(self):
        N1 = self.gcsize(S)-1
        self._commit_transaction_with_one_reference(N1)

    def test_commit_transaction_with_one_reference_128(self):
        self._commit_transaction_with_one_reference(128)

    def test_commit_transaction_with_graph(self):
        self.gc.tls_page_size = 1
        sr1, sr1_adr = self.malloc(SR)
        sr2, sr2_adr = self.malloc(SR)
        self.select_thread(1)
        tr1_adr = self.gc.stm_writebarrier(sr1_adr)   # make a local copy
        tr2_adr = self.gc.stm_writebarrier(sr2_adr)   # make a local copy
        tr1 = llmemory.cast_adr_to_ptr(tr1_adr, lltype.Ptr(SR))
        tr2 = llmemory.cast_adr_to_ptr(tr2_adr, lltype.Ptr(SR))
        tr3, tr3_adr = self.malloc(SR)
        tr4, tr4_adr = self.malloc(SR)
        t, t_adr = self.malloc(S)
        #
        tr1.sr2 = tr3; tr1.sr3 = tr1
        tr2.sr2 = tr3; tr2.sr3 = tr3
        tr3.sr2 = tr4; tr3.sr3 = tr2
        tr4.sr2 = tr3; tr4.sr3 = tr3; tr4.s1 = t
        #
        for i in range(4):
            self.malloc(S)     # forgotten
        #
        main_tls = self.gc.main_thread_tls
        old_value = main_tls.nursery_free
        #
        self.gc.collector.commit_transaction()
        #
        assert main_tls.nursery_free - old_value == (
            self.gcsize(SR) + self.gcsize(SR) + self.gcsize(S))
        #
        sr3_adr = self.gc.header(tr3_adr).version
        sr4_adr = self.gc.header(tr4_adr).version
        s_adr   = self.gc.header(t_adr  ).version
        assert len(set([sr3_adr, sr4_adr, s_adr])) == 3
        #
        sr3 = llmemory.cast_adr_to_ptr(sr3_adr, lltype.Ptr(SR))
        sr4 = llmemory.cast_adr_to_ptr(sr4_adr, lltype.Ptr(SR))
        s   = llmemory.cast_adr_to_ptr(s_adr,   lltype.Ptr(S))
        assert tr1.sr2 == sr3; assert tr1.sr3 == sr1     # roots: local obj
        assert tr2.sr2 == sr3; assert tr2.sr3 == sr3     #        is modified
        assert sr3.sr2 == sr4; assert sr3.sr3 == sr2     # non-roots: global
        assert sr4.sr2 == sr3; assert sr4.sr3 == sr3     #      obj is modified
        assert sr4.s1 == s
        #
        self.checkflags(sr1, 1, 1)
        self.checkflags(sr2, 1, 1)
        self.checkflags(sr3, 1, 0, llmemory.NULL)
        self.checkflags(sr4, 1, 0, llmemory.NULL)
        self.checkflags(s  , 1, 0, llmemory.NULL)

    def test_do_get_size(self):
        s1, s1_adr = self.malloc(S)
        assert (repr(self.gc.stm_operations._getsize_fn(s1_adr)) ==
                repr(fake_get_size(s1_adr)))

    def test_id_of_global(self):
        s, s_adr = self.malloc(S)
        i = self.gc.id(s)
        assert i == llmemory.cast_adr_to_int(s_adr)

    def test_id_of_globallocal(self):
        s, s_adr = self.malloc(S)
        self.select_thread(1)
        t_adr = self.gc.stm_writebarrier(s_adr)   # make a local copy
        t = llmemory.cast_adr_to_ptr(t_adr, llmemory.GCREF)
        i = self.gc.id(t)
        assert i == llmemory.cast_adr_to_int(s_adr)
        assert i == self.gc.id(s)
        self.gc.commit_transaction()
        assert i == self.gc.id(s)

    def test_id_of_local_nonsurviving(self):
        self.select_thread(1)
        s, s_adr = self.malloc(S)
        i = self.gc.id(s)
        assert i != llmemory.cast_adr_to_int(s_adr)
        assert i == self.gc.id(s)
        self.gc.commit_transaction()

    def test_id_of_local_surviving(self):
        sr1, sr1_adr = self.malloc(SR)
        self.select_thread(1)
        t2, t2_adr = self.malloc(S)
        tr1_adr = self.gc.stm_writebarrier(sr1_adr)
        assert tr1_adr != sr1_adr
        tr1 = llmemory.cast_adr_to_ptr(tr1_adr, lltype.Ptr(SR))
        tr1.s1 = t2
        i = self.gc.id(t2)
        assert i not in (llmemory.cast_adr_to_int(sr1_adr),
                         llmemory.cast_adr_to_int(t2_adr),
                         llmemory.cast_adr_to_int(tr1_adr))
        assert i == self.gc.id(t2)
        self.gc.commit_transaction()
        s2 = tr1.s1       # tr1 is a root, so not copied yet
        assert s2 and s2 != t2
        assert self.gc.id(s2) == i

    def test_hash_of_global(self):
        s, s_adr = self.malloc(S)
        i = self.gc.identityhash(s)
        assert i == mangle_hash(llmemory.cast_adr_to_int(s_adr))

    def test_hash_of_globallocal(self):
        s, s_adr = self.malloc(S)
        self.select_thread(1)
        t_adr = self.gc.stm_writebarrier(s_adr)   # make a local copy
        t = llmemory.cast_adr_to_ptr(t_adr, llmemory.GCREF)
        i = self.gc.identityhash(t)
        assert i == mangle_hash(llmemory.cast_adr_to_int(s_adr))
        assert i == self.gc.identityhash(s)
        self.gc.commit_transaction()
        assert i == self.gc.identityhash(s)

    def test_hash_of_local_nonsurviving(self):
        self.select_thread(1)
        s, s_adr = self.malloc(S)
        i = self.gc.identityhash(s)
        assert i != mangle_hash(llmemory.cast_adr_to_int(s_adr))
        assert i == self.gc.identityhash(s)
        self.gc.commit_transaction()

    def test_hash_of_local_surviving(self):
        sr1, sr1_adr = self.malloc(SR)
        self.select_thread(1)
        t2, t2_adr = self.malloc(S)
        tr1_adr = self.gc.stm_writebarrier(sr1_adr)
        assert tr1_adr != sr1_adr
        tr1 = llmemory.cast_adr_to_ptr(tr1_adr, lltype.Ptr(SR))
        tr1.s1 = t2
        i = self.gc.identityhash(t2)
        assert i not in map(mangle_hash,
                        (llmemory.cast_adr_to_int(sr1_adr),
                         llmemory.cast_adr_to_int(t2_adr),
                         llmemory.cast_adr_to_int(tr1_adr)))
        assert i == self.gc.identityhash(t2)
        self.gc.commit_transaction()
        s2 = tr1.s1       # tr1 is a root, so not copied yet
        assert s2 and s2 != t2
        assert self.gc.identityhash(s2) == i

    def test_weakref_to_global(self):
        swr1, swr1_adr = self.malloc(SWR)
        s2, s2_adr = self.malloc(S)
        self.select_thread(1)
        wr1, wr1_adr = self.malloc(WR, weakref=True)
        wr1.wadr = s2_adr
        twr1_adr = self.gc.stm_writebarrier(swr1_adr)
        twr1 = llmemory.cast_adr_to_ptr(twr1_adr, lltype.Ptr(SWR))
        twr1.wr = wr1
        self.gc.commit_transaction()
        wr2 = twr1.wr      # twr1 is a root, so not copied yet
        assert wr2 and wr2 != wr1
        assert wr2.wadr == s2_adr   # survives

    def test_weakref_to_local_dying(self):
        swr1, swr1_adr = self.malloc(SWR)
        self.select_thread(1)
        t2, t2_adr = self.malloc(S)
        wr1, wr1_adr = self.malloc(WR, weakref=True)
        wr1.wadr = t2_adr
        twr1_adr = self.gc.stm_writebarrier(swr1_adr)
        twr1 = llmemory.cast_adr_to_ptr(twr1_adr, lltype.Ptr(SWR))
        twr1.wr = wr1
        self.gc.commit_transaction()
        wr2 = twr1.wr      # twr1 is a root, so not copied yet
        assert wr2 and wr2 != wr1
        assert wr2.wadr == llmemory.NULL   # dies

    def test_weakref_to_local_surviving(self):
        sr1, sr1_adr = self.malloc(SR)
        swr1, swr1_adr = self.malloc(SWR)
        self.select_thread(1)
        t2, t2_adr = self.malloc(S)
        wr1, wr1_adr = self.malloc(WR, weakref=True)
        wr1.wadr = t2_adr
        twr1_adr = self.gc.stm_writebarrier(swr1_adr)
        twr1 = llmemory.cast_adr_to_ptr(twr1_adr, lltype.Ptr(SWR))
        twr1.wr = wr1
        tr1_adr = self.gc.stm_writebarrier(sr1_adr)
        tr1 = llmemory.cast_adr_to_ptr(tr1_adr, lltype.Ptr(SR))
        tr1.s1 = t2
        t2.a = 4242
        self.gc.commit_transaction()
        wr2 = twr1.wr      # twr1 is a root, so not copied yet
        assert wr2 and wr2 != wr1
        assert wr2.wadr and wr2.wadr != t2_adr       # survives
        s2 = llmemory.cast_adr_to_ptr(wr2.wadr, lltype.Ptr(S))
        assert s2.a == 4242
        assert s2 == tr1.s1   # tr1 is a root, so not copied yet

    def test_normalize_global_null(self):
        a = self.gc.stm_normalize_global(llmemory.NULL)
        assert a == llmemory.NULL

    def test_normalize_global_already_global(self):
        sr1, sr1_adr = self.malloc(SR)
        a = self.gc.stm_normalize_global(sr1_adr)
        assert a == sr1_adr

    def test_normalize_global_purely_local(self):
        self.select_thread(1)
        sr1, sr1_adr = self.malloc(SR)
        a = self.gc.stm_normalize_global(sr1_adr)
        assert a == sr1_adr

    def test_normalize_global_local_copy(self):
        sr1, sr1_adr = self.malloc(SR)
        self.select_thread(1)
        tr1_adr = self.gc.stm_writebarrier(sr1_adr)
        a = self.gc.stm_normalize_global(sr1_adr)
        assert a == sr1_adr
        a = self.gc.stm_normalize_global(tr1_adr)
        assert a == sr1_adr